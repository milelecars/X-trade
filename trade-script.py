"""
REAL-TIME CRYPTO SCANNER - Binance WebSocket (Windows Compatible)
TRUE real-time monitoring - alerts sent THE SECOND signal triggers

3-LAYER SIGNAL LOGIC:
  Layer 2 - CROSS CONFIRM   : EMA9 OR EMA26 freshly crossed MA44 within last 8
                               candles, AND the other is already on the correct side
  Layer 3 - SETUP CANDLE    : RSI + EMA positions + bearish/bullish + close vs MAs
  Layer 4 - TRIGGER CANDLE  : MA44 sloping in right direction (3 candles) + open gap

  Entry price = OPEN of the trigger candle

PARAMETERS:
  RSI_SHORT_MAX  : 55   (catches sharp drops before RSI reacts)
  CROSS_LOOKBACK : 8    (checks up to 2 hours back for cross)
  MA44 slope     : 3 candles (current direction, not historical)
  Cooldown       : 2h 30min per symbol
"""

import websocket
import json
import requests
import pandas as pd
from datetime import datetime
import time
from collections import deque
import logging
import sys
import io
import os

# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
    GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")
    USE_AI_ANALYSIS    = True

    RSI_PERIOD         = 14
    EMA_SHORT          = 9
    EMA_LONG           = 26
    MA_PERIOD          = 44
    RSI_LONG_MIN       = 45.1
    RSI_LONG_MAX       = 85
    RSI_SHORT_MIN      = 10
    RSI_SHORT_MAX      = 55
    SL_PERCENT         = 0.5
    TP_PERCENT         = 1.5
    CROSS_LOOKBACK     = 8
    SLOPE_LOOKBACK     = 3

    CANDLE_INTERVAL     = '15m'
    HISTORY_BARS        = 120
    ALERT_COOLDOWN      = 2.5 * 60 * 60   # 2h 30min
    SEND_INSTANT_ALERTS = True

SYMBOLS = ['BTCUSDT']

# ============================================================================
# LOGGING
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

def send_telegram_alert(message):
    if not Config.SEND_INSTANT_ALERTS:
        return False
    url     = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': Config.TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True
    }
    try:
        r = requests.post(url, json=payload, timeout=5)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram failed: {e}")
        return False


def get_ai_analysis(symbol, signal, data):
    if not Config.USE_AI_ANALYSIS or not Config.GEMINI_API_KEY:
        if signal == 'LONG':
            return (
                "EMA crossover confirms momentum shift.\n"
                f"RSI ({data['rsi']:.1f}) shows buying pressure without overbought.\n"
                "MA44 trend supports directional bias.\nRisk-reward is favorable."
            )
        else:
            return (
                "EMA crossover signals downside momentum.\n"
                f"RSI ({data['rsi']:.1f}) shows selling pressure without extreme oversold.\n"
                "MA44 slope confirms trend direction.\nRisk-reward is favorable."
            )
    try:
        import google.generativeai as genai
        genai.configure(api_key=Config.GEMINI_API_KEY)
        model  = genai.GenerativeModel("models/gemini-2.0-flash-exp")
        prompt = (
            f"Analyze this {signal} trading signal for {symbol} in 2-3 sentences.\n"
            f"Entry: {data['entry']:.2f}, RSI: {data['rsi']:.2f}, "
            f"EMA9: {data['ema9']:.2f}, EMA26: {data['ema26']:.2f}, MA44: {data['ma44']:.2f}.\n"
            "Explain trend alignment, momentum, and risk."
        )
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return "Insight unavailable."


def format_signal_alert(symbol, signal, data):
    entry = data['entry']
    emoji = 'LONG' if signal == 'LONG' else 'SHORT'
    icon  = 'GREEN' if signal == 'LONG' else 'RED'

    return f"""{'GREEN' if signal=='LONG' else 'RED'} <b>{signal} - {symbol}</b>

Entry:     ${entry:.2f}
Stop Loss: ${data['sl']:.2f}
Take Prof: ${data['tp']:.2f}

Indicators:
- RSI:    {data['rsi']:.2f}
- EMA 9:  ${data['ema9']:.2f}
- EMA 26: ${data['ema26']:.2f}
- MA 44:  ${data['ma44']:.2f}
- R/R:    1:{Config.TP_PERCENT/Config.SL_PERCENT:.1f}

Do NOT enter if price is
Less than  ${entry*0.998:.2f}
More than ${entry*1.002:.2f}

<pre>INSIGHT
{get_ai_analysis(symbol, signal, data)}</pre>

<pre>DISCLAIMER
This isn't financial advice -
I'm documenting how I allocate my own capital
so you can see how a serious operator approaches alternative markets.</pre>""".strip()

# ============================================================================
# INDICATORS
# ============================================================================

class Ind:
    @staticmethod
    def rsi(closes, period=14):
        if len(closes) < period + 1:
            return 50.0
        s = pd.Series(closes)
        d = s.diff()
        g = d.where(d > 0, 0).rolling(period).mean()
        l = (-d.where(d < 0, 0)).rolling(period).mean()
        return float((100 - 100 / (1 + g/l)).iloc[-1])

    @staticmethod
    def ema(closes, period):
        if len(closes) < period:
            return closes[-1] if closes else 0.0
        return float(pd.Series(closes).ewm(span=period, adjust=False).mean().iloc[-1])

    @staticmethod
    def sma(closes, period):
        if len(closes) < period:
            return closes[-1] if closes else 0.0
        return sum(closes[-period:]) / period

# ============================================================================
# 3-LAYER LOGIC (Layer 1 sideways filter removed — slope check covers it)
# ============================================================================


def had_cross_below_ma44(closes):
    mp, cl = Config.MA_PERIOD, Config.CROSS_LOOKBACK
    if len(closes) < mp + cl + 2:
        return False
    e9  = Ind.ema(closes, Config.EMA_SHORT)
    e26 = Ind.ema(closes, Config.EMA_LONG)
    m44 = Ind.sma(closes, mp)
    e9b  = e9  < m44
    e26b = e26 < m44
    if not e9b and not e26b:
        return False
    e9x = e26x = False
    for k in range(1, cl + 1):
        cn = closes[:-k+1] if k > 1 else closes
        cp = closes[:-k]
        if len(cn) < mp or len(cp) < mp:
            continue
        e9n  = Ind.ema(cn, Config.EMA_SHORT);  e9p  = Ind.ema(cp, Config.EMA_SHORT)
        e26n = Ind.ema(cn, Config.EMA_LONG);   e26p = Ind.ema(cp, Config.EMA_LONG)
        m44n = Ind.sma(cn, mp);               m44p = Ind.sma(cp, mp)
        if not e9x  and e9p  >= m44p and e9n  < m44n: e9x  = True
        if not e26x and e26p >= m44p and e26n < m44n: e26x = True
    return (e9x and e26b) or (e26x and e9b)


def had_cross_above_ma44(closes):
    mp, cl = Config.MA_PERIOD, Config.CROSS_LOOKBACK
    if len(closes) < mp + cl + 2:
        return False
    e9  = Ind.ema(closes, Config.EMA_SHORT)
    e26 = Ind.ema(closes, Config.EMA_LONG)
    m44 = Ind.sma(closes, mp)
    e9a  = e9  > m44
    e26a = e26 > m44
    if not e9a and not e26a:
        return False
    e9x = e26x = False
    for k in range(1, cl + 1):
        cn = closes[:-k+1] if k > 1 else closes
        cp = closes[:-k]
        if len(cn) < mp or len(cp) < mp:
            continue
        e9n  = Ind.ema(cn, Config.EMA_SHORT);  e9p  = Ind.ema(cp, Config.EMA_SHORT)
        e26n = Ind.ema(cn, Config.EMA_LONG);   e26p = Ind.ema(cp, Config.EMA_LONG)
        m44n = Ind.sma(cn, mp);               m44p = Ind.sma(cp, mp)
        if not e9x  and e9p  <= m44p and e9n  > m44n: e9x  = True
        if not e26x and e26p <= m44p and e26n > m44n: e26x = True
    return (e9x and e26a) or (e26x and e9a)


def check_setup(closes, opens):
    mp, cl = Config.MA_PERIOD, Config.CROSS_LOOKBACK
    if len(closes) < mp + cl + 5:
        return None
    sc   = closes[-1];  so  = opens[-1]
    rsi  = Ind.rsi(closes, Config.RSI_PERIOD)
    ema9 = Ind.ema(closes, Config.EMA_SHORT)
    e26  = Ind.ema(closes, Config.EMA_LONG)
    m44  = Ind.sma(closes, mp)

    if (had_cross_above_ma44(closes) and
        Config.RSI_LONG_MIN <= rsi <= Config.RSI_LONG_MAX and
        ema9 > e26 and ema9 > m44 and e26 > m44 and
        sc > so and sc > ema9 and sc > e26 and sc > m44):
        return 'LONG'

    if (had_cross_below_ma44(closes) and
        Config.RSI_SHORT_MIN <= rsi <= Config.RSI_SHORT_MAX and
        ema9 < e26 and ema9 < m44 and e26 < m44 and
        sc < so and sc < ema9 and sc < e26 and sc < m44):
        return 'SHORT'

    return None


def check_trigger(closes, opens, direction):
    mp, sl = Config.MA_PERIOD, Config.SLOPE_LOOKBACK
    if len(closes) < mp + sl + 2:
        return None
    m44_now  = Ind.sma(closes[:-1], mp)
    m44_prev = Ind.sma(closes[:-1-sl], mp)
    topen    = opens[-1]
    sopen    = opens[-2]
    if direction == 'LONG'  and m44_now > m44_prev and topen > sopen: return topen
    if direction == 'SHORT' and m44_now < m44_prev and topen < sopen: return topen
    return None

# ============================================================================
# CANDLE MANAGER
# ============================================================================

class CandleManager:

    def __init__(self, symbol, max_bars=120):
        self.symbol          = symbol
        self.max_bars        = max_bars
        self.timestamps      = deque(maxlen=max_bars)
        self.opens           = deque(maxlen=max_bars)
        self.highs           = deque(maxlen=max_bars)
        self.lows            = deque(maxlen=max_bars)
        self.closes          = deque(maxlen=max_bars)
        self.volumes         = deque(maxlen=max_bars)
        self.current_candle  = None
        self.last_alert_time = 0
        self.pending_dir     = None
        self.pending_data    = None
        self.load_history()

    def load_history(self):
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={'symbol': self.symbol, 'interval': Config.CANDLE_INTERVAL,
                        'limit': self.max_bars},
                timeout=10
            )
            if r.status_code != 200:
                logger.error(f"{self.symbol}: HTTP {r.status_code}")
                return
            for c in r.json():
                self.timestamps.append(int(c[0]))
                self.opens.append(float(c[1]))
                self.highs.append(float(c[2]))
                self.lows.append(float(c[3]))
                self.closes.append(float(c[4]))
                self.volumes.append(float(c[5]))
            logger.info(f"{self.symbol}: Loaded {len(self.closes)} bars")
        except Exception as e:
            logger.error(f"{self.symbol}: load_history error - {e}")

    def update_tick(self, price, timestamp=None):
        if timestamp is None:
            timestamp = int(time.time() * 1000)
        candle_ms    = 15 * 60 * 1000
        candle_start = (timestamp // candle_ms) * candle_ms

        if not self.current_candle or candle_start > self.current_candle['ts']:
            if self.current_candle:
                self.timestamps.append(self.current_candle['ts'])
                self.opens.append(self.current_candle['open'])
                self.highs.append(self.current_candle['high'])
                self.lows.append(self.current_candle['low'])
                self.closes.append(self.current_candle['close'])
                self.volumes.append(0)
                return True
            self.current_candle = {
                'ts': candle_start, 'open': price,
                'high': price, 'low': price, 'close': price
            }
        else:
            c = self.current_candle
            c['high']  = max(c['high'], price)
            c['low']   = min(c['low'],  price)
            c['close'] = price
        return False

    def check_signal(self):
        closes = list(self.closes)
        opens  = list(self.opens)
        min_bars = Config.MA_PERIOD + Config.CROSS_LOOKBACK + 10

        if len(closes) < min_bars:
            return None, None

        # CHECK A: Layer 4 trigger
        if self.pending_dir is not None:
            entry = check_trigger(closes, opens, self.pending_dir)
            if entry is not None:
                if time.time() - self.last_alert_time >= Config.ALERT_COOLDOWN:
                    d  = self.pending_dir
                    pd = self.pending_data
                    sl = entry * (1 - Config.SL_PERCENT/100) if d == 'LONG' else entry * (1 + Config.SL_PERCENT/100)
                    tp = entry * (1 + Config.TP_PERCENT/100) if d == 'LONG' else entry * (1 - Config.TP_PERCENT/100)
                    alert = {'entry': entry, 'sl': sl, 'tp': tp,
                             'rsi': pd['rsi'], 'ema9': pd['ema9'],
                             'ema26': pd['ema26'], 'ma44': pd['ma44']}
                    self.pending_dir  = None
                    self.pending_data = None
                    self.last_alert_time = time.time()
                    return d, alert
                else:
                    logger.info(f"{self.symbol}: Signal suppressed - cooldown active")
            else:
                logger.info(f"{self.symbol}: Pending {self.pending_dir} cancelled (Layer 4 failed)")
            self.pending_dir  = None
            self.pending_data = None

        # CHECK B: Layers 1-3 setup
        if len(closes) < min_bars + 1:
            return None, None

        sc = closes[:-1]
        so = opens[:-1]
        direction = check_setup(sc, so)

        if direction is not None:
            rsi  = Ind.rsi(sc, Config.RSI_PERIOD)
            ema9 = Ind.ema(sc, Config.EMA_SHORT)
            e26  = Ind.ema(sc, Config.EMA_LONG)
            m44  = Ind.sma(sc, Config.MA_PERIOD)
            self.pending_dir  = direction
            self.pending_data = {'rsi': rsi, 'ema9': ema9, 'ema26': e26, 'ma44': m44}
            logger.info(
                f"{self.symbol}: {direction} setup (Layers 1-3) - "
                f"RSI={rsi:.1f}, EMA9={ema9:.2f}, EMA26={e26:.2f}, MA44={m44:.2f}"
            )

        return None, None

# ============================================================================
# WEBSOCKET SCANNER
# ============================================================================

class RealtimeScanner:

    def __init__(self, symbols):
        self.symbols         = symbols
        self.candle_managers = {s: CandleManager(s) for s in symbols}
        self.ws              = None
        self.running         = False

    def on_message(self, ws, message):
        try:
            data   = json.loads(message)
            td     = data['data'] if 'stream' in data else data
            symbol = td['s']
            price  = float(td['c'])
            if symbol in self.candle_managers:
                mgr = self.candle_managers[symbol]
                if mgr.update_tick(price):
                    signal, alert_data = mgr.check_signal()
                    if signal and alert_data:
                        msg = format_signal_alert(symbol, signal, alert_data)
                        if send_telegram_alert(msg):
                            logger.info(f"[SIGNAL] {symbol} {signal} @ ${alert_data['entry']:.4f}")
        except Exception as e:
            logger.error(f"on_message error: {e}")

    def on_error(self, ws, error):
        if str(error).strip():
            logger.error(f"WebSocket error: {error}")

    def on_close(self, ws, code, msg):
        logger.warning("WebSocket closed")
        if self.running:
            logger.info("Reconnecting in 5s...")
            time.sleep(5)
            self.start()

    def on_open(self, ws):
        logger.info("[OK] WebSocket connected - REAL-TIME monitoring active!")

    def start(self):
        self.running = True
        streams    = '/'.join(f"{s.lower()}@ticker" for s in self.symbols)
        ws_url     = f"wss://stream.binance.com:9443/stream?streams={streams}"
        logger.info("=" * 70)
        logger.info("REAL-TIME CRYPTO SCANNER - 4-Layer Signal Logic")
        logger.info("=" * 70)
        logger.info(f"Monitoring : {len(self.symbols)} symbols")
        logger.info(f"Layers     : Sideways > Cross (8c) > Setup > Trigger (3c slope)")
        logger.info(f"RSI SHORT  : 10-55  |  Cooldown: 2h 30min")
        logger.info(f"Telegram   : {'ENABLED' if Config.SEND_INSTANT_ALERTS else 'DISABLED'}")
        logger.info("=" * 70)
        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=self.on_message, on_error=self.on_error,
            on_close=self.on_close,    on_open=self.on_open
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
        print("ERROR: Please set TELEGRAM_BOT_TOKEN!")
        return
    scanner = RealtimeScanner(SYMBOLS)
    try:
        scanner.start()
    except KeyboardInterrupt:
        print("\nScanner stopped.")
        scanner.stop()


def send_test_signal():
    test = {
        'entry': 65724.10, 'sl': 65395.38, 'tp': 66709.95,
        'rsi': 52.34, 'ema9': 65800.00, 'ema26': 65978.00, 'ma44': 66044.00
    }
    send_telegram_alert(format_signal_alert('BTCUSDT', 'LONG', test))
    print("Test signal sent!")

# send_test_signal()

if __name__ == "__main__":
    main()