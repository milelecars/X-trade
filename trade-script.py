"""
REAL-TIME CRYPTO SCANNER - Binance WebSocket (Windows Compatible)
TRUE real-time monitoring - alerts sent THE SECOND signal triggers

SIGNAL LOGIC — Logic No. 2b (MA44 Bounce, no RSI, no crossovers):
  SHORT: A bearish (red) candle forms directly below a MA44 that has been
         continuously falling for each of the last 4 candles.
         Body must be strictly below MA44 (no sloppiness).
         Wick (high-low) >= 0.35%.  Body top within 0.75% of MA44.
  LONG:  Mirror of above — bullish candle above a continuously rising MA44.
  Entry: Open of the NEXT candle after the validation candle closes.
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


    # ── Strategy Parameters (Logic No. 2b — MA44 Bounce) ──────────────────
    MA_PERIOD      = 44
    SL_PERCENT     = 0.5
    TP_PERCENT     = 1.5

    # Validation candle filters
    MIN_WICK_PCT   = 0.0035   # 0.35% wick (high-to-low) minimum
    MAX_DIST_PCT   = 0.0075   # 0.75% max distance of body edge from MA44
    SLOPE_LOOKBACK = 4        # MA44 must be continuously falling/rising for 4 candles

    # Real-time Settings
    CANDLE_INTERVAL = '15m'
    HISTORY_BARS    = 100

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

# ============================================================================
# CANDLE MANAGER
# ============================================================================

class CandleManager:
    """Manages real-time candle data — Logic No. 2b (MA44 Bounce)"""

    def __init__(self, symbol: str, max_bars: int = 100):
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
        # When a validation candle passes all conditions, we set setup_pending
        # and fire the entry on the OPEN of the very next candle.
        self.setup_pending      = False
        self.pending_signal     = None    # 'LONG' or 'SHORT'
        self.pending_setup_data = None    # {'ma44': float}
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
    # SIGNAL LOGIC — Logic No. 2b (MA44 Bounce)
    # Called once per closed candle (when update_tick returns True)
    # =========================================================================

    def _ma44_series(self, closes_list: list) -> list:
        """
        Return SLOPE_LOOKBACK+1 consecutive MA44 values (oldest first)
        so we can check whether each step is strictly lower or higher.
        """
        n      = len(closes_list)
        series = []
        for k in range(Config.SLOPE_LOOKBACK, -1, -1):
            end = n - k if k > 0 else n
            val = IndicatorEngine.calculate_sma(closes_list[:end], Config.MA_PERIOD)
            series.append(val)
        return series   # length = SLOPE_LOOKBACK + 1

    def _check_validation_candle(
        self,
        closes_list: list,
        opens_list:  list,
        highs_list:  list,
        lows_list:   list,
    ):
        """
        Evaluate the last closed candle as a potential validation candle.

        SHORT — all must pass:
          1. Bearish candle  (close < open)
          2. MA44 continuously falling over last 4 candles (each value < previous)
          3. Entire body strictly below MA44  (body_top < ma44, no sloppiness)
          4. Body top within 0.75% of MA44   ((ma44 - body_top) / ma44 <= 0.0075)
          5. Wick >= 0.35% of high           ((high - low) / high >= 0.0035)

        LONG — mirror of above.

        Returns 'LONG', 'SHORT', or None.
        """
        if len(closes_list) < Config.MA_PERIOD + Config.SLOPE_LOOKBACK + 2:
            return None

        c_close = closes_list[-1]
        c_open  = opens_list[-1]
        c_high  = highs_list[-1]
        c_low   = lows_list[-1]

        ma44 = IndicatorEngine.calculate_sma(closes_list, Config.MA_PERIOD)
        if not ma44:
            return None

        # MA44 monotonic slope check
        ma44_series = self._ma44_series(closes_list)
        continuously_down = all(
            ma44_series[i] > ma44_series[i + 1]
            for i in range(len(ma44_series) - 1)
        )
        continuously_up = all(
            ma44_series[i] < ma44_series[i + 1]
            for i in range(len(ma44_series) - 1)
        )

        body_top    = max(c_open, c_close)
        body_bottom = min(c_open, c_close)
        wick_pct    = (c_high - c_low) / c_high
        dist_abs    = ma44 * Config.MAX_DIST_PCT

        # ── SHORT ─────────────────────────────────────────────────────────────
        if c_close < c_open:
            if (
                continuously_down and
                body_top < ma44 and
                (ma44 - body_top) <= dist_abs and
                wick_pct >= Config.MIN_WICK_PCT
            ):
                return 'SHORT'

        # ── LONG ──────────────────────────────────────────────────────────────
        if c_close > c_open:
            if (
                continuously_up and
                body_bottom > ma44 and
                (body_bottom - ma44) <= dist_abs and
                wick_pct >= Config.MIN_WICK_PCT
            ):
                return 'LONG'

        return None

    def check_signal(self) -> tuple:
        """
        Called on every newly closed candle.

        STEP A — Entry fire (validation candle was the previous bar):
            The new candle has just opened.
            Entry = open of this new candle (current_candle['open']).
            Fire the signal now.

        STEP B — Validation check (candle that just closed):
            Run _check_validation_candle() on the closed candle.
            If it passes → mark setup_pending = True.
            Entry will fire on the NEXT call to check_signal (Step A).
        """
        if len(self.closes) < Config.MA_PERIOD + Config.SLOPE_LOOKBACK + 5:
            return None, None

        closes_list = list(self.closes)
        opens_list  = list(self.opens)
        highs_list  = list(self.highs)
        lows_list   = list(self.lows)

        # ── STEP A: fire entry if last candle was a validation candle ─────────
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

            # Entry = open of the candle that just started
            entry = (
                self.current_candle['open']
                if self.current_candle
                else opens_list[-1]
            )

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

        # ── STEP B: check if candle that just closed is a validation candle ───
        direction = self._check_validation_candle(
            closes_list, opens_list, highs_list, lows_list
        )

        if direction is not None:
            ma44_now = IndicatorEngine.calculate_sma(closes_list, Config.MA_PERIOD)
            self.setup_pending      = True
            self.pending_signal     = direction
            self.pending_setup_data = {'ma44': ma44_now}
            logger.info(
                f"{self.symbol}: {direction} validation candle — "
                f"entry fires on next candle open  (MA44={ma44_now:.2f})"
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
        logger.info("REAL-TIME CRYPTO SCANNER  —  Logic No. 2b  (MA44 Bounce)")
        logger.info("=" * 80)
        logger.info(f"Monitoring  : {len(self.symbols)} symbols")
        logger.info(f"Logic       : MA44 Bounce — no RSI, no crossovers")
        logger.info(f"SHORT       : bearish candle below continuously-falling MA44 (4 candles)")
        logger.info(f"LONG        : bullish candle above continuously-rising  MA44 (4 candles)")
        logger.info(f"Conditions  : body strictly outside MA44 | wick >= 0.35% | dist <= 0.75%")
        logger.info(f"Entry price : open of candle immediately after validation candle")
        logger.info(f"Cooldown    : {Config.ALERT_COOLDOWN//60}min per symbol")
        logger.info(f"Telegram    : {'ENABLED' if Config.SEND_INSTANT_ALERTS else 'DISABLED'}")
        logger.info("=" * 80)

        startup_msg = (
            "🚀 <b>REAL-TIME Scanner Started — Logic No. 2b</b>\n\n"
            "⚡ <b>MA44 BOUNCE LOGIC ACTIVE</b>\n\n"
            f"✅ Monitoring: {len(self.symbols)} crypto pairs\n"
            "✅ No RSI | No crossovers\n"
            "✅ Entry = open of candle after validation candle\n"
            "✅ Cooldown: 2h30m per symbol\n\n"
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