import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Optional, List, Set
from contextlib import asynccontextmanager
from collections import deque

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.gzip import GZipMiddleware
import httpx
from bs4 import BeautifulSoup

# ============== Global State ==============
MAX_HISTORY = 1441
MAX_USD_HISTORY = 11

history: deque = deque(maxlen=MAX_HISTORY)
usd_idr_history: deque = deque(maxlen=MAX_USD_HISTORY)
last_buy: Optional[int] = None
shown_updates: Set[str] = set()
treasury_info: str = "Belum ada info treasury."

update_event = asyncio.Event()
usd_idr_update_event = asyncio.Event()
treasury_info_update_event = asyncio.Event()

telegram_app = None

# ============== Utility Functions ==============
def format_rupiah(nominal: int) -> str:
    try:
        return "{:,}".format(int(nominal)).replace(",", ".")
    except:
        return str(nominal)

def parse_price_to_float(price_str: str) -> Optional[float]:
    try:
        return float(price_str.replace('.', '').replace(',', '.'))
    except:
        return None

def calc_20jt(h: dict) -> str:
    try:
        gram = 20000000 / h["buying_rate"]
        val = int(gram * h["selling_rate"] - 19315000)
        gram_str = f"{gram:,.4f}".replace(",", ".")
        if val > 0:
            return f"+{format_rupiah(val)}🟢➺{gram_str}gr"
        elif val < 0:
            return f"-{format_rupiah(abs(val))}🔴➺{gram_str}gr"
        return f"0➖➺{gram_str}gr"
    except:
        return "-"

def calc_30jt(h: dict) -> str:
    try:
        gram = 30000000 / h["buying_rate"]
        val = int(gram * h["selling_rate"] - 28980000)
        gram_str = f"{gram:,.4f}".replace(",", ".")
        if val > 0:
            return f"+{format_rupiah(val)}🟢➺{gram_str}gr"
        elif val < 0:
            return f"-{format_rupiah(abs(val))}🔴➺{gram_str}gr"
        return f"0➖➺{gram_str}gr"
    except:
        return "-"

def build_history_data() -> List[dict]:
    return [
        {
            "buying_rate": format_rupiah(h["buying_rate"]),
            "selling_rate": format_rupiah(h["selling_rate"]),
            "status": h["status"],
            "created_at": h["created_at"],
            "jt20": calc_20jt(h),
            "jt30": calc_30jt(h)
        }
        for h in history
    ]

def build_usd_idr_data() -> List[dict]:
    return [{"price": h["price"], "time": h["time"]} for h in usd_idr_history]

# ============== HTTP Client ==============
http_client: Optional[httpx.AsyncClient] = None

async def get_http_client() -> httpx.AsyncClient:
    global http_client
    if http_client is None or http_client.is_closed:
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            follow_redirects=True
        )
    return http_client

async def close_http_client():
    global http_client
    if http_client and not http_client.is_closed:
        await http_client.aclose()
        http_client = None

# ============== API Functions ==============
async def fetch_treasury_price() -> Optional[dict]:
    try:
        client = await get_http_client()
        response = await client.post(
            "https://api.treasury.id/api/v1/antigrvty/gold/rate",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://treasury.id",
                "Referer": "https://treasury.id/",
            }
        )
        if response.status_code == 200:
            return response.json()
    except:
        pass
    return None

async def fetch_usd_idr_price() -> Optional[str]:
    try:
        client = await get_http_client()
        response = await client.get(
            "https://www.google.com/finance/quote/USD-IDR",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html",
                "Cache-Control": "no-cache",
            },
            cookies={
                "CONSENT": "YES+cb.20231208-04-p0.en+FX+410",
                "SOCS": "CAISHAgCEhJnd3NfMjAyMzEyMDgtMF9SQzEaAmVuIAEaBgiA_LmqBg",
            }
        )
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            price_div = soup.find("div", class_="YMlKec fxKbKc")
            if price_div:
                return price_div.text.strip()
    except:
        pass
    return None

# ============== Background Tasks ==============
async def api_loop():
    global last_buy, shown_updates
    
    await asyncio.sleep(1)
    
    while True:
        try:
            result = await fetch_treasury_price()
            
            if result:
                data = result.get("data", {})
                buying_rate = data.get("buying_rate")
                selling_rate = data.get("selling_rate")
                updated_at = data.get("updated_at")
                
                if buying_rate and selling_rate and updated_at:
                    buying_rate = int(float(buying_rate))
                    selling_rate = int(float(selling_rate))
                    
                    if updated_at not in shown_updates:
                        status = "➖"
                        if last_buy is not None:
                            if buying_rate > last_buy:
                                status = "🚀"
                            elif buying_rate < last_buy:
                                status = "🔻"
                        
                        history.append({
                            "buying_rate": buying_rate,
                            "selling_rate": selling_rate,
                            "status": status,
                            "created_at": updated_at
                        })
                        last_buy = buying_rate
                        shown_updates.add(updated_at)
                        
                        if len(shown_updates) > 5000:
                            shown_updates = {updated_at}
                        
                        update_event.set()
            
            await asyncio.sleep(0.1)
            
        except asyncio.CancelledError:
            break
        except:
            await asyncio.sleep(1)

async def usd_idr_loop():
    await asyncio.sleep(2)
    
    while True:
        try:
            price_str = await fetch_usd_idr_price()
            
            if price_str:
                if not usd_idr_history or usd_idr_history[-1]["price"] != price_str:
                    wib_now = datetime.utcnow() + timedelta(hours=7)
                    usd_idr_history.append({
                        "price": price_str,
                        "time": wib_now.strftime("%H:%M:%S")
                    })
                    usd_idr_update_event.set()
            
            await asyncio.sleep(1)
            
        except asyncio.CancelledError:
            break
        except:
            await asyncio.sleep(2)

# ============== Telegram Bot ==============
async def start_telegram_bot():
    global telegram_app, treasury_info
    
    try:
        from telegram.ext import ApplicationBuilder, CommandHandler
        from telegram import Update
        from telegram.ext import ContextTypes
    except ImportError:
        return None
    
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        return None
    
    async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Bot aktif! Gunakan /atur <teks> untuk mengubah info treasury.")
    
    async def atur_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global treasury_info
        text = update.message.text.partition(' ')[2]
        if text:
            treasury_info = text.replace("  ", "&nbsp;&nbsp;").replace("\n", "<br>")
            treasury_info_update_event.set()
            await update.message.reply_text("Info Treasury berhasil diubah!")
        else:
            await update.message.reply_text("Gunakan: /atur <kalimat info>")
    
    try:
        telegram_app = ApplicationBuilder().token(token).build()
        telegram_app.add_handler(CommandHandler("start", start_handler))
        telegram_app.add_handler(CommandHandler("atur", atur_handler))
        
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message"])
        return telegram_app
    except:
        return None

async def stop_telegram_bot():
    global telegram_app
    if telegram_app:
        try:
            await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()
        except:
            pass
        telegram_app = None

# ============== HTML Template ==============
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
    <title>Harga Emas Treasury</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css"/>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; background: #fff; color: #222; transition: background 0.3s, color 0.3s; }
        table.dataTable thead th { font-weight: bold; }
        th.waktu, td.waktu { width: 150px; min-width: 100px; max-width: 180px; white-space: nowrap; text-align: left; }
        th.profit, td.profit { width: 154px; min-width: 80px; max-width: 160px; white-space: nowrap; text-align: left; }
        .dark-mode { background: #181a1b !important; color: #e0e0e0 !important; }
        .dark-mode #jam { color: #ffb300 !important; }
        .dark-mode table.dataTable { background: #23272b !important; color: #e0e0e0 !important; }
        .dark-mode table.dataTable thead th { background: #23272b !important; color: #ffb300 !important; }
        .dark-mode table.dataTable tbody td { background: #23272b !important; color: #e0e0e0 !important; }
        .theme-toggle-btn { padding: 0; border: none; border-radius: 50%; background: #222; color: #fff; font-weight: bold; cursor: pointer; transition: background 0.3s, color 0.3s; font-size: 1.5em; width: 44px; height: 44px; display: flex; align-items: center; justify-content: center; }
        .theme-toggle-btn:hover { background: #444; }
        .dark-mode .theme-toggle-btn { background: #ffb300; color: #222; }
        .dark-mode .theme-toggle-btn:hover { background: #ffd54f; }
        .container-flex { display: flex; gap: 15px; margin-top: 10px; flex-wrap: wrap; }
        #usdIdrRealtime { width: 248px; border: 1px solid #ccc; padding: 10px; height: 370px; overflow-y: auto; }
        #priceList li { margin-bottom: 1px; }
        .time { color: gray; font-size: 0.9em; margin-left: 10px; }
        #currentPrice { color: red; font-weight: bold; }
        .dark-mode #currentPrice { color: #00E124; text-shadow: 1px 1px #00B31C; }
        #tabel tbody tr:first-child td { color: red !important; font-weight: bold; }
        .dark-mode #tabel tbody tr:first-child td { color: #00E124 !important; font-weight: bold; }
        #footerApp { width: 100%; overflow: hidden; position: fixed; bottom: 0; left: 0; background: transparent; text-align: center; z-index: 100; padding: 8px 0; }
        .marquee-text { display: inline-block; color: #F5274D; animation: marquee 70s linear infinite; font-weight: bold; }
        .dark-mode .marquee-text { color: #B232B2; font-weight: bold; }
        @keyframes marquee { 0% { transform: translateX(100vw); } 100% { transform: translateX(-100vw); } }
        #isiTreasury { white-space: pre-line; color: red; font-weight: bold; max-height: 376px; overflow-y: auto; scrollbar-width: none; -ms-overflow-style: none; word-break: break-word; }
        #isiTreasury::-webkit-scrollbar { display: none; }
        .dark-mode #isiTreasury { color: #00E124; text-shadow: 1px 1px #00B31C; }
        #ingfo { width: 218px; border: 1px solid #ccc; padding: 10px; height: 378px; overflow-y: auto; }
        .loading-text { color: #999; font-style: italic; }
    </style>
</head>
<body>
    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px;">
        <h2 style="margin:0;">MONITORING Harga Emas Treasury</h2>
        <button class="theme-toggle-btn" id="themeBtn" onclick="toggleTheme()" title="Ganti Tema">🌙</button>
    </div>
    <div id="jam" style="font-size:1.3em; color:#ff1744; font-weight:bold; margin-bottom:15px;"></div>
    <table id="tabel" class="display" style="width:100%">
        <thead>
            <tr>
                <th class="waktu">Waktu</th>
                <th>Data Transaksi</th>
                <th class="profit">Est. cuan 20 JT ➺ gr</th>
                <th class="profit">Est. cuan 30 JT ➺ gr</th>
            </tr>
        </thead>
        <tbody></tbody>
    </table>
    <div style="margin-top:40px;">
        <h3>Chart Harga Emas (XAU/USD)</h3>
        <div id="tradingview_chart"></div>
    </div>
    <div class="container-flex">
        <div>
            <h3 style="display:block; margin-top:30px;">Chart Harga USD/IDR Investing - Jangka Waktu 15 Menit</h3>
            <div style="overflow:hidden; height:370px; width:620px; border:1px solid #ccc; border-radius:6px;">
                <iframe src="https://sslcharts.investing.com/index.php?force_lang=54&pair_ID=2138&timescale=900&candles=80&style=candles" width="618" height="430" style="margin-top:-62px; border:0;" loading="lazy" scrolling="no" frameborder="0" allowtransparency="true"></iframe>
            </div>
        </div>
        <div>
            <h3>Harga USD/IDR Google Finance</h3>
            <div id="usdIdrRealtime" style="margin-top:0; padding-top:2px;">
                <p>Harga saat ini: <span id="currentPrice" class="loading-text">Memuat data...</span></p>
                <h4>Harga Terakhir:</h4>
                <ul id="priceList" style="list-style:none; padding-left:0; max-height:275px; overflow-y:auto;">
                    <li class="loading-text">Menunggu data...</li>
                </ul>
            </div>
        </div>
    </div>
    <div class="container-flex">
        <div>
            <h3 style="display:block; margin-top:30px;">Kalender Ekonomi</h3>
            <div style="overflow:hidden; height:470px; width:100%; border:1px solid #ccc; border-radius:6px;">
                <iframe src="https://sslecal2.investing.com?columns=exc_flags,exc_currency,exc_importance,exc_actual,exc_forecast,exc_previous&category=_employment,_economicActivity,_inflation,_centralBanks,_confidenceIndex&importance=3&features=datepicker,timezone,timeselector,filters&countries=5,37,48,35,17,36,26,12,72&calType=week&timeZone=27&lang=54" width="650" height="467" loading="lazy" frameborder="0" allowtransparency="true" marginwidth="0" marginheight="0"></iframe>
            </div>
        </div>
        <div>
            <h3>Sekilas Ingfo Treasury</h3>
            <div id="ingfo" style="margin-top:0; padding-top:2px;">
                <ul id="isiTreasury" style="list-style:none; padding-left:0;"></ul>
            </div>
        </div>
    </div>
    <script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
    <script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
    <script src="https://s3.tradingview.com/tv.js"></script>
    <script>
        new TradingView.widget({
            "width": "100%",
            "height": 400,
            "symbol": "OANDA:XAUUSD",
            "interval": "15",
            "timezone": "Asia/Jakarta",
            "theme": localStorage.getItem('theme') === 'dark' ? 'dark' : 'light',
            "style": "1",
            "locale": "id",
            "toolbar_bg": "#f1f3f6",
            "enable_publishing": false,
            "hide_top_toolbar": false,
            "save_image": false,
            "container_id": "tradingview_chart"
        });

        var table = $('#tabel').DataTable({
            "pageLength": 4,
            "lengthMenu": [4, 8, 18, 48, 88, 888, 1441],
            "order": [],
            "columns": [
                { "data": "waktu" },
                { "data": "all" },
                { "data": "jt20" },
                { "data": "jt30" }
            ],
            "language": {
                "emptyTable": "Menunggu data harga emas dari Treasury...",
                "zeroRecords": "Tidak ada data yang cocok"
            }
        });

        function updateTable(history) {
            if (!history || history.length === 0) return;
            history.sort(function(a, b) { return new Date(b.created_at) - new Date(a.created_at); });
            var dataArr = history.map(function(d) {
                return {
                    waktu: d.created_at,
                    all: (d.status || "➖") + " | Harga Beli: " + d.buying_rate + " | Jual: " + d.selling_rate,
                    jt20: d.jt20,
                    jt30: d.jt30
                };
            });
            table.clear().rows.add(dataArr).draw(false);
            table.page('first').draw(false);
        }

        function updateUsdIdrPrice(history) {
            var c = document.getElementById("currentPrice");
            var p = document.getElementById("priceList");
            if (!history || history.length === 0) {
                c.textContent = "Menunggu data...";
                c.className = "loading-text";
                p.innerHTML = '<li class="loading-text">Menunggu data...</li>';
                return;
            }
            c.className = "";
            function parseHarga(str) { return parseFloat(str.trim().replace(/\./g, '').replace(',', '.')); }
            var reversed = history.slice().reverse();
            var rowIconCurrent = "➖";
            if (reversed.length > 1) {
                var now = parseHarga(reversed[0].price);
                var prev = parseHarga(reversed[1].price);
                if (now > prev) rowIconCurrent = "🚀";
                else if (now < prev) rowIconCurrent = "🔻";
            }
            c.innerHTML = reversed[0].price + " " + rowIconCurrent;
            p.innerHTML = "";
            for (var i = 0; i < reversed.length; i++) {
                var rowIcon = "➖";
                if (i === 0 && reversed.length > 1) {
                    var now = parseHarga(reversed[0].price);
                    var prev = parseHarga(reversed[1].price);
                    if (now > prev) rowIcon = "🟢";
                    else if (now < prev) rowIcon = "🔴";
                } else if (i < reversed.length - 1) {
                    var now = parseHarga(reversed[i].price);
                    var next = parseHarga(reversed[i+1].price);
                    if (now > next) rowIcon = "🟢";
                    else if (now < next) rowIcon = "🔴";
                }
                var li = document.createElement("li");
                li.innerHTML = reversed[i].price + ' <span class="time">(' + reversed[i].time + ')</span> ' + rowIcon;
                p.appendChild(li);
            }
        }

        function updateTreasuryInfo(info) {
            document.getElementById("isiTreasury").innerHTML = info || "Belum ada info treasury.";
        }

        var ws = null, reconnectAttempts = 0;
        function connectWS() {
            var protocol = location.protocol === "https:" ? "wss:" : "ws:";
            ws = new WebSocket(protocol + "//" + location.host + "/ws");
            ws.onopen = function() { reconnectAttempts = 0; };
            ws.onmessage = function(event) {
                try {
                    var data = JSON.parse(event.data);
                    if (data.ping) return;
                    if (data.history) updateTable(data.history);
                    if (data.usd_idr_history) updateUsdIdrPrice(data.usd_idr_history);
                    if (data.treasury_info !== undefined) updateTreasuryInfo(data.treasury_info);
                } catch (e) {}
            };
            ws.onclose = function() {
                reconnectAttempts++;
                setTimeout(connectWS, Math.min(1000 * Math.pow(1.5, reconnectAttempts - 1), 30000));
            };
            ws.onerror = function() {};
        }
        connectWS();

        function updateJam() {
            var now = new Date();
            var tgl = now.toLocaleDateString('id-ID', { day: '2-digit', month: 'long', year: 'numeric' });
            var jam = now.toLocaleTimeString('id-ID', { hour12: false });
            document.getElementById("jam").textContent = tgl + " " + jam + " WIB";
        }
        setInterval(updateJam, 1000);
        updateJam();

        function toggleTheme() {
            var body = document.body;
            var btn = document.getElementById('themeBtn');
            body.classList.toggle('dark-mode');
            if (body.classList.contains('dark-mode')) {
                btn.textContent = "☀️";
                localStorage.setItem('theme', 'dark');
            } else {
                btn.textContent = "🌙";
                localStorage.setItem('theme', 'light');
            }
        }
        (function() {
            var theme = localStorage.getItem('theme');
            var btn = document.getElementById('themeBtn');
            if (theme === 'dark') {
                document.body.classList.add('dark-mode');
                btn.textContent = "☀️";
            }
        })();
    </script>
    <footer id="footerApp"><span class="marquee-text">&copy;2025 ~ahmadkholil~</span></footer>
</body>
</html>"""

# ============== FastAPI Application ==============
@asynccontextmanager
async def lifespan(app: FastAPI):
    task1 = asyncio.create_task(api_loop())
    task2 = asyncio.create_task(usd_idr_loop())
    await start_telegram_bot()
    
    yield
    
    task1.cancel()
    task2.cancel()
    await stop_telegram_bot()
    await close_http_client()
    try:
        await asyncio.gather(task1, task2, return_exceptions=True)
    except:
        pass

app = FastAPI(title="Gold Price Monitor", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=HTML_TEMPLATE)

@app.get("/health")
async def health():
    return {"status": "ok", "history": len(history), "usd": len(usd_idr_history)}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    last_h = history[-1]["created_at"] if history else None
    last_u = usd_idr_history[-1]["price"] if usd_idr_history else None
    last_t = treasury_info

    try:
        await websocket.send_text(json.dumps({
            "history": build_history_data(),
            "usd_idr_history": build_usd_idr_data(),
            "treasury_info": treasury_info
        }))

        while True:
            tasks = [
                asyncio.create_task(update_event.wait()),
                asyncio.create_task(usd_idr_update_event.wait()),
                asyncio.create_task(treasury_info_update_event.wait())
            ]
            
            done, pending = await asyncio.wait(tasks, timeout=30.0, return_when=asyncio.FIRST_COMPLETED)
            
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

            if update_event.is_set():
                update_event.clear()
            if usd_idr_update_event.is_set():
                usd_idr_update_event.clear()
            if treasury_info_update_event.is_set():
                treasury_info_update_event.clear()

            curr_h = history[-1]["created_at"] if history else None
            curr_u = usd_idr_history[-1]["price"] if usd_idr_history else None
            curr_t = treasury_info

            if curr_h != last_h or curr_u != last_u or curr_t != last_t:
                last_h, last_u, last_t = curr_h, curr_u, curr_t
                await websocket.send_text(json.dumps({
                    "history": build_history_data(),
                    "usd_idr_history": build_usd_idr_data(),
                    "treasury_info": treasury_info
                }))
            else:
                await websocket.send_text('{"ping":true}')

    except WebSocketDisconnect:
        pass
    except:
        pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="error")
