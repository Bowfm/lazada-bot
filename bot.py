"""Bot Telegram báo kèo giảm giá sâu trên Lazada.vn (mọi shop)."""
import argparse, html, logging, os, random, sqlite3, time
import requests

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT"]
THRESHOLD = float(os.getenv("THRESHOLD", "70"))          # % giảm tối thiểu
MAX_PCT = float(os.getenv("MAX_PCT", "98"))               # trên mức này coi là giá gốc ảo
MIN_PRICE = int(os.getenv("MIN_PRICE", "20000"))          # VND, bỏ hàng quá rẻ
MIN_RATING = float(os.getenv("MIN_RATING", "0"))          # 0 = không lọc
PAGES = int(os.getenv("PAGES", "3"))                      # số trang / từ khóa
INTERVAL_MIN = int(os.getenv("INTERVAL_MIN", "15"))
COOKIE = os.getenv("LAZADA_COOKIE", "")                   # tùy chọn, giảm bị captcha
KEYWORDS = [k.strip() for k in os.getenv(
    "KEYWORDS",
    "điện thoại,tai nghe,laptop,chuột,bàn phím,giày,áo thun,túi xách,đồng hồ,"
    "nồi chiên,máy xay,đồ chơi,mỹ phẩm,sạc dự phòng,loa bluetooth,balo",
).split(",") if k.strip()]

DB = os.getenv("DB_PATH", "seen.db")
log = logging.getLogger("lazbot")


class Blocked(Exception):
    pass


def db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS sent(item_id TEXT, price INTEGER, ts INTEGER, "
                "PRIMARY KEY(item_id, price))")
    return con


def tg(method, **payload):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=payload, timeout=20)
    return r.ok


def notify_text(text):
    tg("sendMessage", chat_id=CHAT_ID, text=text)


def num(x):
    try:
        return float(str(x).replace(",", "").replace(".", "") if isinstance(x, str) and x.count(".") > 1
                     else str(x).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def parse_item(it):
    price = num(it.get("price"))
    orig = num(it.get("originalPrice"))
    if price <= 0 or orig <= 0 or orig <= price:
        return None
    pct = (1 - price / orig) * 100
    url = it.get("itemUrl") or it.get("productUrl") or ""
    if url.startswith("//"):
        url = "https:" + url
    img = it.get("image") or ""
    if img.startswith("//"):
        img = "https:" + img
    return {
        "id": str(it.get("itemId") or it.get("nid") or url),
        "name": it.get("name", "?"),
        "price": int(price), "orig": int(orig), "pct": pct,
        "url": url, "img": img,
        "shop": it.get("sellerName", ""),
        "rating": num(it.get("ratingScore")),
        "sold": it.get("itemSoldCntShow", ""),
    }


def fetch(session, keyword, page):
    r = session.get("https://www.lazada.vn/catalog/", timeout=25, params={
        "ajax": "true", "q": keyword, "page": page, "sort": "popularity"})
    if r.status_code != 200 or "punish" in r.text[:2000].lower():
        raise Blocked(f"HTTP {r.status_code}")
    try:
        data = r.json()
        return data["mods"]["listItems"]
    except (ValueError, KeyError, TypeError):
        raise Blocked("phản hồi không phải JSON hợp lệ")


def good(p):
    return (THRESHOLD <= p["pct"] <= MAX_PCT and p["price"] >= MIN_PRICE
            and p["rating"] >= MIN_RATING and p["url"])


def send_deal(p):
    cap = (f"🔥 <b>-{p['pct']:.0f}%</b> | {html.escape(p['name'][:150])}\n"
           f"💰 <b>{p['price']:,}đ</b> <s>{p['orig']:,}đ</s>\n"
           f"🏪 {html.escape(p['shop'])}"
           + (f" | ⭐ {p['rating']:.1f}" if p["rating"] else "")
           + (f" | đã bán {html.escape(str(p['sold']))}" if p["sold"] else "")
           + f"\n🔗 {p['url']}")
    if p["img"] and tg("sendPhoto", chat_id=CHAT_ID, photo=p["img"], caption=cap, parse_mode="HTML"):
        return
    tg("sendMessage", chat_id=CHAT_ID, text=cap, parse_mode="HTML")


def cycle(con, session, state):
    found = 0
    kws = KEYWORDS[:]
    random.shuffle(kws)
    for kw in kws:
        for page in range(1, PAGES + 1):
            try:
                items = fetch(session, kw, page)
                state["blocks"] = 0
            except Blocked as e:
                state["blocks"] += 1
                log.warning("Bị chặn (%s) ở '%s' trang %d", e, kw, page)
                if state["blocks"] == 3 and not state["warned"]:
                    notify_text("⚠️ Lazada đang chặn bot (captcha). Bot sẽ chờ lâu hơn rồi thử lại. "
                                "Có thể thêm LAZADA_COOKIE (cookie trình duyệt của bạn) để giảm bị chặn.")
                    state["warned"] = True
                if state["blocks"] >= 3:
                    return found
                break
            for it in items:
                p = parse_item(it)
                if not p or not good(p):
                    continue
                if con.execute("SELECT 1 FROM sent WHERE item_id=? AND price=?", (p["id"], p["price"])).fetchone():
                    continue
                send_deal(p)
                con.execute("INSERT OR IGNORE INTO sent VALUES(?,?,?)", (p["id"], p["price"], int(time.time())))
                con.commit()
                found += 1
                time.sleep(1)  # tránh rate limit của Telegram
            time.sleep(random.uniform(3, 8))
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="chạy 1 vòng rồi thoát (cron / GitHub Actions)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "vi-VN,vi;q=0.9",
        "Referer": "https://www.lazada.vn/",
    })
    if COOKIE:
        s.headers["Cookie"] = COOKIE

    con = db()
    state = {"blocks": 0, "warned": False}
    while True:
        n = cycle(con, s, state)
        log.info("Vòng xong: %d kèo mới", n)
        if args.once:
            break
        wait = INTERVAL_MIN * 60 * (4 if state["blocks"] >= 3 else 1)
        if state["blocks"] >= 3:
            state["blocks"] = 0
        time.sleep(wait + random.uniform(0, 60))


if __name__ == "__main__":
    main()
