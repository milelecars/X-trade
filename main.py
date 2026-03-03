"""
REAL-TIME CRYPTO SCANNER - Binance WebSocket (Windows Compatible)
TRUE real-time monitoring - alerts sent THE SECOND signal triggers

SIGNAL LOGIC — 3-Layer (btc_backtest, Layer 1 sideways filter removed):

  LAYER 2 — Cross confirmation (last 8 candles):
    LONG : EMA9 freshly crossed above MA44 within last 8 candles
           AND EMA26 is currently above MA44 (already there is fine)
    SHORT: EMA9 freshly crossed below MA44 within last 8 candles
           AND EMA26 is currently below MA44 (already there is fine)
    (Relaxed: 1 freshly crossed + the other already on correct side)

  LAYER 3 — Setup candle (candle that just closed):
    LONG:
      1. RSI 45.1–85
      2. EMA9 > EMA26
      3. EMA9 > MA44  AND  EMA26 > MA44
      4. Bullish candle (close > open)
      5. Close > EMA9, EMA26, MA44

    SHORT:
      1. RSI 10–55
      2. EMA9 < EMA26
      3. EMA9 < MA44  AND  EMA26 < MA44
      4. Bearish candle (close < open)
      5. Close < EMA9, EMA26, MA44

  LAYER 4 — Trigger candle (fires at open of NEXT candle):
    LONG : MA44(setup) > MA44(3 candles before setup)  AND  open > setup open
    SHORT: MA44(setup) < MA44(3 candles before setup)  AND  open < setup open
    Entry = open of trigger candle

  Cooldown: 2 hours 30 minutes per symbol.

- WebSocket streaming (instant price updates)
- Checks indicators on every candle close
- Telegram alert within 1 second of signal
- Monitors 40+ crypto pairs
- Completely FREE
- No rate limits

For CRYPTO ONLY (uses Binance)
"""

import websocket
import json
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
from collections import deque
import logging
import sys
import io
import os
# from dotenv import load_dotenv

# load_dotenv()

# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Strategy Parameters (3-Layer Logic — no sideways filter) ──────────
    MA_PERIOD          = 44
    EMA_SHORT          = 9
    EMA_LONG           = 26
    RSI_PERIOD         = 14

    RSI_LONG_MIN       = 45.1
    RSI_LONG_MAX       = 85
    RSI_SHORT_MIN      = 10
    RSI_SHORT_MAX      = 55       # expanded from 45 to catch sharp drops

    SL_PERCENT         = 0.5
    TP_PERCENT         = 1.5

    CROSS_LOOKBACK     = 8        # candles to look back for EMA/MA44 cross
    SLOPE_CANDLES      = 3        # MA44 slope: compare now vs 3 candles back

    # Real-time Settings
    CANDLE_INTERVAL = '15m'
    HISTORY_BARS    = 150         # enough warmup for all indicators

    # Alert Settings
    ALERT_COOLDOWN      = 150 * 60   # 2h30m in seconds (150 minutes)
    SEND_INSTANT_ALERTS = True

# ============================================================================
# YOUR 40+ CRYPTO SYMBOLS
# ============================================================================

SYMBOLS = [
    'BTCUSDT',
]

# ============================================================================
# LOGGING - Windows Compatible
# ============================================================================

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    handlers=[
        logging.FileHandler('realtime_scanner.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# TELEGRAM
# ============================================================================

def send_telegram_alert(message: str) -> bool:
    """Send INSTANT Telegram alert"""
    if not Config.SEND_INSTANT_ALERTS:
        return False

    url     = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id':                  Config.TELEGRAM_CHAT_ID,
        'text':                     message,
        'parse_mode':               'HTML',
        'disable_web_page_preview': True
    }

    try:
        response = requests.post(url, json=payload, timeout=5)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram failed: {e}")
        return False


def format_signal_alert(symbol: str, signal: str, data: dict) -> str:
    emoji = '🟢' if signal == 'LONG' else '🔴'
    now   = datetime.now().strftime('%H:%M %d %b')
    rr    = f"1:{Config.TP_PERCENT / Config.SL_PERCENT:.0f}"
    lines = [
        f"{emoji} {signal} {symbol}  {now}",
        f"Entry  {data['entry']:.2f}",
        f"SL     {data['sl']:.2f}",
        f"TP     {data['tp']:.2f}",
        f"R/R    {rr}",
    ]
    return '\n'.join(lines)


# ============================================================================
# INDICATOR CALCULATOR
# ============================================================================

class IndicatorEngine:
    """Fast indicator calculations"""

    @staticmethod
    def calculate_sma(closes: list, period: int) -> float:
        if len(closes) < period:
            return closes[-1] if closes else 0.0
        return sum(closes[-period:]) / period

    @staticmethod
    def calculate_ema(closes: list, period: int) -> float:
        if len(closes) < period:
            return closes[-1] if closes else 0.0
        prices = pd.Series(closes)
        return float(prices.ewm(span=period, adjust=False).mean().iloc[-1])

    @staticmethod
    def calculate_rsi(closes: list, period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        prices = pd.Series(closes)
        delta  = prices.diff()
        gain   = delta.where(delta > 0, 0).rolling(window=period).mean()
        loss   = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs     = gain / loss
        rsi    = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1])

# ============================================================================
# CANDLE MANAGER
# ============================================================================

class CandleManager:
    """Manages real-time candle data — 3-Layer Signal Logic (no sideways filter)"""

    def __init__(self, symbol: str, max_bars: int = 150):
        self.symbol   = symbol
        self.max_bars = max_bars

        self.timestamps = deque(maxlen=max_bars)
        self.opens      = deque(maxlen=max_bars)
        self.highs      = deque(maxlen=max_bars)
        self.lows       = deque(maxlen=max_bars)
        self.closes     = deque(maxlen=max_bars)
        self.volumes    = deque(maxlen=max_bars)

        self.current_candle  = None
        self.last_alert_time = 0

        # ── Signal State ──────────────────────────────────────────────────────
        # Layer 2+3 (setup candle) sets setup_pending = True.
        # Layer 4 (trigger candle) fires entry on the NEXT candle open.
        self.setup_pending      = False
        self.pending_signal     = None    # 'LONG' or 'SHORT'
        self.pending_setup_data = None    # {'ma44': float, 'setup_open': float}
        # ─────────────────────────────────────────────────────────────────────

        self.load_history()

    def load_history(self):
        """Load historical candles"""
        try:
            url    = "https://api.binance.com/api/v3/klines"
            params = {
                'symbol':   self.symbol,
                'interval': Config.CANDLE_INTERVAL,
                'limit':    self.max_bars
            }

            response = requests.get(url, params=params, timeout=10)

            if response.status_code != 200:
                logger.error(f"{self.symbol}: HTTP {response.status_code}")
                return False

            candles = response.json()

            if not isinstance(candles, list):
                logger.error(f"{self.symbol}: Invalid response format")
                return False

            if len(candles) < 50:
                logger.error(f"{self.symbol}: Insufficient data ({len(candles)} bars)")
                return False

            for candle in candles:
                try:
                    self.timestamps.append(int(candle[0]))
                    self.opens.append(float(candle[1]))
                    self.highs.append(float(candle[2]))
                    self.lows.append(float(candle[3]))
                    self.closes.append(float(candle[4]))
                    self.volumes.append(float(candle[5]))
                except (ValueError, IndexError, TypeError) as e:
                    logger.error(f"{self.symbol}: Parse error - {e}")
                    return False

            logger.info(f"{self.symbol}: Loaded {len(self.closes)} bars")
            return True

        except requests.exceptions.RequestException as e:
            logger.error(f"{self.symbol}: Network error - {e}")
            return False
        except Exception as e:
            logger.error(f"{self.symbol}: Unexpected error - {e}")
            return False

    def update_tick(self, price: float, timestamp: int = None):
        """
        Update with new price tick.
        Returns True when a new 15-min candle has just closed.
        """
        if timestamp is None:
            timestamp = int(time.time() * 1000)

        candle_ms    = 15 * 60 * 1000
        candle_start = (timestamp // candle_ms) * candle_ms

        if not self.current_candle or candle_start > self.current_candle['timestamp']:
            if self.current_candle:
                self.timestamps.append(self.current_candle['timestamp'])
                self.opens.append(self.current_candle['open'])
                self.highs.append(self.current_candle['high'])
                self.lows.append(self.current_candle['low'])
                self.closes.append(self.current_candle['close'])
                self.volumes.append(self.current_candle['volume'])
                return True

            self.current_candle = {
                'timestamp': candle_start,
                'open':  price,
                'high':  price,
                'low':   price,
                'close': price,
                'volume': 0
            }
        else:
            if self.current_candle:
                self.current_candle['high']  = max(self.current_candle['high'], price)
                self.current_candle['low']   = min(self.current_candle['low'],  price)
                self.current_candle['close'] = price

        return False

    # =========================================================================
    # SIGNAL LOGIC — 3-Layer (Layer 1 sideways filter removed)
    # Called once per closed candle (when update_tick returns True)
    # =========================================================================

    def _layer2_had_cross_above_ma44(self, closes: list) -> bool:
        """
        Layer 2 LONG — Relaxed cross confirmation.
        EMA9 freshly crossed above MA44 within last CROSS_LOOKBACK candles
        AND EMA26 currently above MA44.
        OR EMA26 freshly crossed above MA44 AND EMA9 currently above MA44.
        """
        if len(closes) < Config.MA_PERIOD + Config.CROSS_LOOKBACK + 2:
            return False

        ema9_crossed = ema26_crossed = False
        for k in range(1, Config.CROSS_LOOKBACK + 1):
            cn = closes[:-k + 1] if k > 1 else closes
            cp = closes[:-k]
            if len(cn) < Config.MA_PERIOD or len(cp) < Config.MA_PERIOD:
                continue
            e9n  = IndicatorEngine.calculate_ema(cn, Config.EMA_SHORT)
            e9p  = IndicatorEngine.calculate_ema(cp, Config.EMA_SHORT)
            e26n = IndicatorEngine.calculate_ema(cn, Config.EMA_LONG)
            e26p = IndicatorEngine.calculate_ema(cp, Config.EMA_LONG)
            m44n = IndicatorEngine.calculate_sma(cn, Config.MA_PERIOD)
            m44p = IndicatorEngine.calculate_sma(cp, Config.MA_PERIOD)
            if not ema9_crossed  and (e9p  <= m44p) and (e9n  > m44n): ema9_crossed  = True
            if not ema26_crossed and (e26p <= m44p) and (e26n > m44n): ema26_crossed = True
            if ema9_crossed and ema26_crossed:
                return True

        # Relaxed: 1 freshly crossed + other currently on correct side
        ema9_now  = IndicatorEngine.calculate_ema(closes, Config.EMA_SHORT)
        ema26_now = IndicatorEngine.calculate_ema(closes, Config.EMA_LONG)
        ma44_now  = IndicatorEngine.calculate_sma(closes, Config.MA_PERIOD)
        return (ema9_crossed and ema26_now > ma44_now) or (ema26_crossed and ema9_now > ma44_now)

    def _layer2_had_cross_below_ma44(self, closes: list) -> bool:
        """
        Layer 2 SHORT — Relaxed cross confirmation.
        EMA9 freshly crossed below MA44 within last CROSS_LOOKBACK candles
        AND EMA26 currently below MA44.
        OR EMA26 freshly crossed below MA44 AND EMA9 currently below MA44.
        """
        if len(closes) < Config.MA_PERIOD + Config.CROSS_LOOKBACK + 2:
            return False

        ema9_crossed = ema26_crossed = False
        for k in range(1, Config.CROSS_LOOKBACK + 1):
            cn = closes[:-k + 1] if k > 1 else closes
            cp = closes[:-k]
            if len(cn) < Config.MA_PERIOD or len(cp) < Config.MA_PERIOD:
                continue
            e9n  = IndicatorEngine.calculate_ema(cn, Config.EMA_SHORT)
            e9p  = IndicatorEngine.calculate_ema(cp, Config.EMA_SHORT)
            e26n = IndicatorEngine.calculate_ema(cn, Config.EMA_LONG)
            e26p = IndicatorEngine.calculate_ema(cp, Config.EMA_LONG)
            m44n = IndicatorEngine.calculate_sma(cn, Config.MA_PERIOD)
            m44p = IndicatorEngine.calculate_sma(cp, Config.MA_PERIOD)
            if not ema9_crossed  and (e9p  >= m44p) and (e9n  < m44n): ema9_crossed  = True
            if not ema26_crossed and (e26p >= m44p) and (e26n < m44n): ema26_crossed = True
            if ema9_crossed and ema26_crossed:
                return True

        # Relaxed: 1 freshly crossed + other currently on correct side
        ema9_now  = IndicatorEngine.calculate_ema(closes, Config.EMA_SHORT)
        ema26_now = IndicatorEngine.calculate_ema(closes, Config.EMA_LONG)
        ma44_now  = IndicatorEngine.calculate_sma(closes, Config.MA_PERIOD)
        return (ema9_crossed and ema26_now < ma44_now) or (ema26_crossed and ema9_now < ma44_now)

    def _layer3_check_setup_candle(self, closes: list, opens: list) -> str | None:
        """
        Layer 3 — Setup candle conditions (includes Layer 2).
        No sideways filter (Layer 1 removed).
        Returns 'LONG', 'SHORT', or None.
        """
        min_bars = Config.MA_PERIOD + Config.CROSS_LOOKBACK + 5
        if len(closes) < min_bars:
            return None

        sc    = closes[-1]
        so    = opens[-1]
        rsi   = IndicatorEngine.calculate_rsi(closes, Config.RSI_PERIOD)
        ema9  = IndicatorEngine.calculate_ema(closes, Config.EMA_SHORT)
        ema26 = IndicatorEngine.calculate_ema(closes, Config.EMA_LONG)
        ma44  = IndicatorEngine.calculate_sma(closes, Config.MA_PERIOD)

        long_setup = (
            self._layer2_had_cross_above_ma44(closes) and
            Config.RSI_LONG_MIN <= rsi <= Config.RSI_LONG_MAX and
            ema9 > ema26 and
            ema9 > ma44 and ema26 > ma44 and
            sc > so and
            sc > ema9 and sc > ema26 and sc > ma44
        )

        short_setup = (
            self._layer2_had_cross_below_ma44(closes) and
            Config.RSI_SHORT_MIN <= rsi <= Config.RSI_SHORT_MAX and
            ema9 < ema26 and
            ema9 < ma44 and ema26 < ma44 and
            sc < so and
            sc < ema9 and sc < ema26 and sc < ma44
        )

        if long_setup:  return 'LONG'
        if short_setup: return 'SHORT'
        return None

    def _layer4_check_trigger(self, closes: list, opens: list, direction: str) -> float | None:
        """
        Layer 4 — Trigger candle conditions.
        MA44 current slope (3-candle window) + open gap vs setup open.
        Returns entry price or None.
        """
        if len(closes) < Config.MA_PERIOD + Config.SLOPE_CANDLES + 2:
            return None

        ma44_now = IndicatorEngine.calculate_sma(closes[:-1], Config.MA_PERIOD)
        ma44_ago = IndicatorEngine.calculate_sma(
            closes[-(Config.SLOPE_CANDLES + 2):-1], Config.MA_PERIOD
        )
        topen = opens[-1]   # trigger candle open (candle that just started)
        sopen = opens[-2]   # setup candle open

        if direction == 'LONG'  and ma44_now > ma44_ago and topen > sopen: return topen
        if direction == 'SHORT' and ma44_now < ma44_ago and topen < sopen: return topen
        return None

    def check_signal(self) -> tuple:
        """
        Called on every newly closed candle.

        STEP A — Layer 4 trigger (setup candle was the previous bar):
            Check MA44 slope and open gap on the candle that just opened.
            If passes → fire entry at this candle's open.

        STEP B — Layer 3 setup check (candle that just closed):
            Layers 2+3 run on the closed candle.
            If passes → mark setup_pending = True.
            Layer 4 fires on the NEXT call (Step A).
        """
        min_bars = Config.MA_PERIOD + Config.CROSS_LOOKBACK + 10
        if len(self.closes) < min_bars:
            return None, None

        closes_list = list(self.closes)
        opens_list  = list(self.opens)

        # ── STEP A: attempt Layer 4 trigger if setup is pending ───────────────
        if self.setup_pending and self.pending_signal and self.pending_setup_data:

            # Cooldown guard (2h30m)
            if time.time() - self.last_alert_time < Config.ALERT_COOLDOWN:
                logger.info(
                    f"{self.symbol}: {self.pending_signal} signal suppressed — "
                    f"cooldown active ({Config.ALERT_COOLDOWN//60}min)"
                )
                self.setup_pending      = False
                self.pending_signal     = None
                self.pending_setup_data = None
                return None, None

            signal     = self.pending_signal
            setup_data = self.pending_setup_data

            entry = self._layer4_check_trigger(closes_list, opens_list, signal)

            if entry is None:
                logger.info(
                    f"{self.symbol}: {signal} setup cancelled — "
                    f"Layer 4 (MA44 slope / open gap) did not confirm"
                )
                self.setup_pending      = False
                self.pending_signal     = None
                self.pending_setup_data = None
                return None, None

            if signal == 'LONG':
                alert_data = {
                    'entry': entry,
                    'sl':    entry * (1 - Config.SL_PERCENT / 100),
                    'tp':    entry * (1 + Config.TP_PERCENT / 100),
                    'ma44':  setup_data['ma44'],
                }
            else:
                alert_data = {
                    'entry': entry,
                    'sl':    entry * (1 + Config.SL_PERCENT / 100),
                    'tp':    entry * (1 - Config.TP_PERCENT / 100),
                    'ma44':  setup_data['ma44'],
                }

            self.setup_pending      = False
            self.pending_signal     = None
            self.pending_setup_data = None
            self.last_alert_time    = time.time()

            return signal, alert_data

        # ── STEP B: check if candle that just closed is a setup candle ────────
        direction = self._layer3_check_setup_candle(closes_list, opens_list)

        if direction is not None:
            ma44_now = IndicatorEngine.calculate_sma(closes_list, Config.MA_PERIOD)
            self.setup_pending      = True
            self.pending_signal     = direction
            self.pending_setup_data = {
                'ma44':       ma44_now,
                'setup_open': opens_list[-1],
            }
            logger.info(
                f"{self.symbol}: {direction} setup candle (Layers 2–3 PASS) — "
                f"awaiting Layer 4 on next candle open  (MA44={ma44_now:.2f})"
            )

        return None, None


# ============================================================================
# WEBSOCKET SCANNER
# ============================================================================

class RealtimeScanner:
    """Real-time scanner"""

    def __init__(self, symbols: list):
        self.symbols         = symbols
        self.candle_managers = {}
        self.ws              = None
        self.running         = False

        for symbol in symbols:
            self.candle_managers[symbol] = CandleManager(symbol)

    def on_message(self, ws, message):
        """Handle incoming WebSocket message"""
        try:
            data = json.loads(message)

            if 'stream' in data:
                ticker_data = data['data']
                symbol      = ticker_data['s']
                price       = float(ticker_data['c'])
            else:
                symbol = data['s']
                price  = float(data['c'])

            if symbol in self.candle_managers:
                manager           = self.candle_managers[symbol]
                new_candle_closed = manager.update_tick(price)

                if new_candle_closed:
                    signal, alert_data = manager.check_signal()

                    if signal and alert_data:
                        msg = format_signal_alert(symbol, signal, alert_data)
                        if send_telegram_alert(msg):
                            logger.info(
                                f"[SIGNAL] {symbol} - {signal} @ ${alert_data['entry']:.4f}"
                            )

        except Exception as e:
            logger.error(f"on_message error: {e}")

    def on_error(self, ws, error):
        if str(error).strip():
            logger.error(f"WebSocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        logger.warning("WebSocket closed")
        if self.running:
            logger.info("Reconnecting in 5 seconds...")
            time.sleep(5)
            self.start()

    def on_open(self, ws):
        logger.info("[OK] WebSocket connected - REAL-TIME monitoring active!")

    def start(self):
        """Start the scanner"""
        self.running = True

        streams    = [f"{s.lower()}@ticker" for s in self.symbols]
        stream_str = '/'.join(streams)
        ws_url     = f"wss://stream.binance.com:9443/stream?streams={stream_str}"

        logger.info("=" * 80)
        logger.info("REAL-TIME CRYPTO SCANNER  —  3-Layer Signal Logic (no sideways filter)")
        logger.info("=" * 80)
        logger.info(f"Monitoring  : {len(self.symbols)} symbols")
        logger.info(f"Layer 2     : Cross confirm    (EMA9/EMA26 vs MA44, relaxed, last {Config.CROSS_LOOKBACK} candles)")
        logger.info(f"Layer 3     : Setup candle     (RSI + EMA order + direction + close vs MAs)")
        logger.info(f"Layer 4     : Trigger candle   (MA44 slope {Config.SLOPE_CANDLES}-candle + open gap)")
        logger.info(f"SHORT RSI   : {Config.RSI_SHORT_MIN}–{Config.RSI_SHORT_MAX}  |  LONG RSI: {Config.RSI_LONG_MIN}–{Config.RSI_LONG_MAX}")
        logger.info(f"Entry price : open of trigger candle (candle after setup)")
        logger.info(f"Cooldown    : {Config.ALERT_COOLDOWN//60}min per symbol")
        logger.info(f"Telegram    : {'ENABLED' if Config.SEND_INSTANT_ALERTS else 'DISABLED'}")
        logger.info("=" * 80)

        startup_msg = (
            "🚀 <b>REAL-TIME Scanner Started — 3-Layer Logic</b>\n\n"
            "⚡ <b>3-LAYER SIGNAL DETECTION ACTIVE</b>\n\n"
            f"✅ Monitoring: {len(self.symbols)} crypto pairs\n"
            f"✅ Layer 2: EMA cross confirmation ({Config.CROSS_LOOKBACK} candles, relaxed)\n"
            f"✅ Layer 3: Setup candle (RSI + EMA + direction)\n"
            f"✅ Layer 4: Trigger candle (MA44 slope + open gap)\n"
            f"✅ Cooldown: {Config.ALERT_COOLDOWN//60}min per symbol\n\n"
            f"⏰ Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        # send_telegram_alert(startup_msg)

        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message = self.on_message,
            on_error   = self.on_error,
            on_close   = self.on_close,
            on_open    = self.on_open
        )

        self.ws.run_forever()

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()

# ============================================================================
# MAIN
# ============================================================================

def main():
    if Config.TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ERROR: Please configure TELEGRAM_BOT_TOKEN!")
        return

    scanner = RealtimeScanner(SYMBOLS)

    try:
        scanner.start()
    except KeyboardInterrupt:
        print("\nScanner stopped by user")
        scanner.stop()

        shutdown_msg = (
            "🛑 <b>Scanner Stopped</b>\n\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        # send_telegram_alert(shutdown_msg)


def send_test_signal():
    """Send a fake signal for testing message format"""
    test_data = {
        'entry': 97500.00,
        'sl':    97012.50,
        'tp':    98962.50,
        'ma44':  97820.00,
    }
    message = format_signal_alert('BTCUSDT', 'SHORT', test_data)
    send_telegram_alert(message)
    print("Test signal sent to Telegram!")


# Uncomment to send test signal immediately
# send_test_signal()

if __name__ == "__main__":
    main()