#!/usr/bin/env python3
"""
A股成交额TOP100 数据报告生成器 v4

新增功能:
  - 每日数据持久化 (data/YYYY-MM-DD.json)
  - HTML 内嵌历史数据，支持日期选择器切换查看
  - 自动执行: 每个交易日 16:00 (通过 automation 设置)

数据来源: 东方财富 + 通达信(mootdx TCP) + WeStock Data CLI + 百度股市通

用法: python generate_report.py
输出: A股成交额TOP100.html + data/YYYY-MM-DD.json
"""

import json, time, random, subprocess, os, sys, glob
from datetime import datetime, timezone, timedelta
import requests

# Windows GBK 控制台兼容: 确保 print emoji 不崩溃
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

OUTPUT_FILE = "index.html"
DATA_DIR = "data"
EM_MIN_INTERVAL = 0.8
_em_last_call = [0.0]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36"
# 禁用代理(沙箱环境兼容)
os.environ['NO_PROXY'] = '*'
EM_SESSION = requests.Session()
EM_SESSION.trust_env = False
EM_SESSION.headers.update({"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"})


def em_get(url, params=None, timeout=20):
    wait = EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.4))
    try:
        r = EM_SESSION.get(url, params=params, timeout=timeout)
        _em_last_call[0] = time.time()
        return r
    except Exception as e:
        _em_last_call[0] = time.time()
        print(f"  [ERROR] HTTP请求失败: {type(e).__name__}: {e}")
        return None


def em_json(url, params=None, timeout=20):
    r = em_get(url, params, timeout=timeout)
    if r is None:
        return {}
    try:
        return r.json()
    except Exception:
        return {}


def get_top100_sina():
    """新浪财经API获取成交额TOP100（备用数据源）

    返回格式与东财API的diff列表兼容:
    [{f2, f3, f6, f12, f13, f14, f20, f100}, ...], total
    """
    try:
        url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
        params = {
            "page": "1", "num": "100", "sort": "amount",
            "asc": "0", "node": "hs_a", "_s_r_a": "sort",
        }
        # 独立Session，不使用EM_SESSION避免节流等待
        s = requests.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": UA})
        r = s.get(url, params=params, timeout=20)
        rows = r.json()
        if not rows or not isinstance(rows, list):
            print(f"  [ERROR] 新浪API请求失败: 返回数据格式异常")
            return [], 0

        def _sf(v, default=0.0):
            """安全转float，处理'-'等非数字值"""
            try:
                return float(v)
            except (ValueError, TypeError):
                return default

        items = []
        for row in rows:
            raw_code = str(row.get("code", "")).strip()
            if not raw_code:
                continue
            # 新浪code格式如"sh603986"/"sz300308"，取后6位为股票代码
            code = raw_code[-6:].zfill(6)
            # f13市场标识: 6/9开头=沪市(1)，其余=深市(0)
            f13 = 1 if code[0] in ("6", "9") else 0
            items.append({
                "f2": _sf(row.get("trade")),            # 当前价格
                "f3": _sf(row.get("changepercent")),    # 涨跌幅(%)
                "f6": _sf(row.get("amount")),           # 成交额(元)
                "f12": code,                            # 股票代码
                "f13": f13,                             # 市场标识
                "f14": row.get("name", ""),             # 股票名称
                "f20": _sf(row.get("mktcap")) * 10000,  # 市值(万元→元)
                "f100": "",                             # 新浪不提供行业
            })
        total = len(items)
        return items, total
    except Exception as e:
        print(f"  [ERROR] 新浪API请求失败: {type(e).__name__}: {e}")
        return [], 0


def get_top100():
    """东财全市场成交额 TOP100（含3次重试，失败后切换新浪财经API）

    Returns:
        (items, total, source): items=股票列表, total=全市场数, source=数据来源标识
    """
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1", "fltt": "2", "invt": "2",
        "fs": "m:0+t:6,m:0+t:13,m:0+t:80,m:1+t:2,m:1+t:23,m:4+t:7,m:4+t:4",
        "fields": "f2,f3,f6,f12,f13,f14,f15,f17,f18,f20,f100,f102,f127",
        "fid": "f6",
    }
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        r = em_get(url, params)
        if r is not None:
            # 有HTTP响应，尝试解析JSON
            try:
                d = r.json()
            except Exception as e:
                print(f"  [WARN] 第{attempt}/{max_retries}次: JSON解析失败 (HTTP {r.status_code}): {e}")
                if attempt < max_retries:
                    wait = 2 + random.uniform(0, 1)
                    print(f"  等待 {wait:.1f}s 后重试...")
                    time.sleep(wait)
                continue
            diff = d.get("data", {}).get("diff", [])
            total = d.get("data", {}).get("total", 0)
            if diff:
                return diff, total, "东方财富"
            # 有响应但无数据（可能是非交易日或盘前）
            print(f"  [WARN] 第{attempt}/{max_retries}次: API返回空数据 (HTTP {r.status_code})")
        else:
            # em_get 已打印异常信息
            print(f"  [WARN] 第{attempt}/{max_retries}次: HTTP请求失败（异常详见上方日志）")
        if attempt < max_retries:
            wait = 2 + random.uniform(0, 1)
            print(f"  等待 {wait:.1f}s 后重试...")
            time.sleep(wait)
    print(f"  [ERROR] 东财API {max_retries}次重试全部失败")
    print(f"  [INFO] 尝试备用数据源（新浪财经）...")
    sina_items, sina_total = get_top100_sina()
    if sina_items:
        print(f"  [OK] 新浪财经API获取成功: {len(sina_items)} 条 (全市场 {sina_total} 只)")
        return sina_items, sina_total, "新浪财经(备用)"
    print(f"  [ERROR] 新浪财经API也失败，所有数据源均不可用")
    return [], 0, ""


def adjust_qfq(df, xdxr_df):
    """将mootdx不复权K线DataFrame转为前复权

    前复权逻辑：除权日之前的所有价格，按除权因子向下调整，
    使除权前后的价格连续可比，消除虚假涨跌幅。

    Args:
        df: mootdx不复权日K线DataFrame
        xdxr_df: mootdx除权除息信息DataFrame

    Returns:
        前复权后的DataFrame
    """
    import math
    if xdxr_df is None or len(xdxr_df) == 0:
        return df

    # 筛选除权除息事件 (category=1)
    fhps = xdxr_df[xdxr_df['category'] == 1].copy()
    if len(fhps) == 0:
        return df

    klines = df.copy()
    adjusted_events = 0

    for _, xr in fhps.iterrows():
        yr, mn, dy = int(xr['year']), int(xr['month']), int(xr['day'])
        xr_date = f'{yr:04d}-{mn:02d}-{dy:02d}'

        fenhong = float(xr.get('fenhong', 0)) if not math.isnan(float(xr.get('fenhong', 0))) else 0
        songzhuangu = float(xr.get('songzhuangu', 0)) if not math.isnan(float(xr.get('songzhuangu', 0))) else 0
        peigu = float(xr.get('peigu', 0)) if not math.isnan(float(xr.get('peigu', 0))) else 0
        peigujia_raw = xr.get('peigujia', None)
        peigujia = float(peigujia_raw) if peigujia_raw is not None and not math.isnan(float(peigujia_raw)) else 0

        fh_per_share = fenhong / 10.0   # 每股分红(元)
        sz_per_share = songzhuangu / 10.0  # 每股送转
        pg_per_share = peigu / 10.0     # 每股配股

        # 前复权公式: new_price = (price - fh + pg * peigujia) / (1 + sz + pg)
        adj_ratio = 1.0 / (1.0 + sz_per_share + pg_per_share)

        adjusted_count = 0
        for i in range(len(klines)):
            row_date = str(klines.iloc[i]['datetime'])[:10]
            if row_date < xr_date:
                for col in ['open', 'close', 'high', 'low']:
                    orig_price = klines.iloc[i][col]
                    # (price - fh + pg * peigujia) / (1 + sz + pg)
                    klines.iloc[i, klines.columns.get_loc(col)] = (
                        (orig_price - fh_per_share + pg_per_share * peigujia) * adj_ratio
                    )
                adjusted_count += 1

        if adjusted_count > 0:
            adjusted_events += 1

    if adjusted_events > 0:
        print(f"    前复权: {adjusted_events}个除权事件, 调整了{adjusted_count}条K线")

    return klines


def get_tencent_klines(code, count=50):
    """腾讯财经API获取前复权日K线, 作为mootdx前复权失败时的后备数据源

    腾讯API直接返回服务端前复权数据, 无需手动计算, 避免xdxr获取失败的问题。

    Args:
        code: 股票代码, 如 "688256"
        count: 获取的K线条数

    Returns:
        前复权K线列表 [{date,open,close,high,low,volume,amount}], 失败返回空列表
    """
    try:
        c = str(code)
        if c.startswith('6') or c.startswith('9'):
            symbol = f'sh{c}'
        else:
            symbol = f'sz{c}'

        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{count},qfq"
        s = requests.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": UA})
        r = s.get(url, timeout=15)
        d = r.json()
        data = d.get("data", {}).get(symbol, {})
        klines = data.get("qfqday", [])
        if not klines:
            klines = data.get("day", [])

        if not klines:
            return []

        result = []
        for kl in klines:
            # 格式: [date, open, close, high, low, volume]
            result.append({
                "date": str(kl[0]),
                "open": float(kl[1]),
                "close": float(kl[2]),
                "high": float(kl[3]),
                "low": float(kl[4]),
                "volume": float(kl[5]) if len(kl) > 5 else 0,
                "amount": 0,
            })
        return result
    except Exception:
        return []


def get_mootdx_klines(code, count=50):
    """mootdx 日K线(前复权), 返回 [{date,open,close,high,low,volume,amount}]

    流程:
      1. mootdx获取不复权K线
      2. mootdx xdxr获取除权除息信息, 计算前复权 (含3次重试)
      3. 若xdxr返回空数据(非异常), 重试
      4. 若mootdx前复权彻底失败(K线获取失败或xdxr失败), 回退到腾讯API获取前复权K线
    """
    mootdx_ok = False
    try:
        from mootdx.quotes import Quotes
        client = Quotes.factory(market="std")
        df = client.bars(symbol=str(code), category=4, offset=count)
        if df is not None and len(df) > 0:
            # 前复权处理（含重试机制，防止xdxr调用偶发失败导致不复权）
            qfq_ok = False
            for retry in range(3):
                try:
                    xdxr = client.xdxr(symbol=str(code))
                    if xdxr is not None and len(xdxr) > 0:
                        df = adjust_qfq(df, xdxr)
                        qfq_ok = True
                        break
                    else:
                        # xdxr返回空数据，可能是连接问题，重试
                        if retry < 2:
                            time.sleep(0.5)
                        else:
                            print(f"    [WARN] {code} xdxr返回空数据(3次重试)")
                except Exception as e:
                    if retry < 2:
                        time.sleep(0.5)
                    else:
                        print(f"    [WARN] {code} 前复权失败(3次重试): {e}")

            if qfq_ok:
                mootdx_ok = True
            else:
                # xdxr失败，使用腾讯API前复权K线作为后备
                print(f"    [INFO] {code} mootdx前复权失败, 尝试腾讯API后备...")
                tx_klines = get_tencent_klines(code, count)
                if tx_klines:
                    print(f"    [OK] {code} 腾讯API后备成功 ({len(tx_klines)}条)")
                    return tx_klines
                print(f"    [WARN] {code} 腾讯API也失败, 使用不复权数据!")
                mootdx_ok = True  # 仍有K线数据，只是未前复权

            result = []
            for row in df.itertuples():
                result.append({
                    "date": str(getattr(row, "datetime", "")),
                    "open": float(getattr(row, "open", 0)),
                    "close": float(getattr(row, "close", 0)),
                    "high": float(getattr(row, "high", 0)),
                    "low": float(getattr(row, "low", 0)),
                    "volume": float(getattr(row, "vol", 0)),
                    "amount": float(getattr(row, "amount", 0)),
                })
            return result
    except Exception:
        pass

    # mootdx K线获取完全失败，使用腾讯API作为最终后备
    if not mootdx_ok:
        tx_klines = get_tencent_klines(code, count)
        if tx_klines:
            print(f"    [OK] {code} 腾讯API获取成功 ({len(tx_klines)}条)")
            return tx_klines

    return []


def get_index_data():
    """通过 WeStock Data CLI 获取主要指数月K/周K"""
    idx_list = [
        ("sh000001", "上证指数"),
        ("sz399001", "深证成指"),
        ("sz399006", "创业板指"),
        ("sh000688", "科创50"),
        ("sz399005", "中小100"),
        ("sh000300", "沪深300"),
    ]
    result = {}
    work_dir = os.path.dirname(os.path.abspath(__file__))

    for sym, name in idx_list:
        try:
            # 日K (最近2天, 用于计算日涨跌)
            r = subprocess.run(
                f"npx -y westock-data-clawhub@1.0.4 kline {sym} --period day --limit 2",
                shell=True, capture_output=True, text=True, timeout=60, cwd=work_dir,
                encoding="utf-8"
            )
            day_rows = _parse_md_table(r.stdout)
            # 月K
            r = subprocess.run(
                f"npx -y westock-data-clawhub@1.0.4 kline {sym} --period month --limit 3",
                shell=True, capture_output=True, text=True, timeout=60, cwd=work_dir,
                encoding="utf-8"
            )
            month_rows = _parse_md_table(r.stdout)
            # 周K
            r = subprocess.run(
                f"npx -y westock-data-clawhub@1.0.4 kline {sym} --period week --limit 3",
                shell=True, capture_output=True, text=True, timeout=60, cwd=work_dir,
                encoding="utf-8"
            )
            week_rows = _parse_md_table(r.stdout)

            lv, dg, mg, wg = 0.0, 0.0, 0.0, 0.0

            # 日涨跌: 从日K线计算
            if day_rows and len(day_rows) >= 1:
                lv = float(day_rows[0].get("last", 0))
                if len(day_rows) >= 2:
                    yesterday = float(day_rows[1].get("last", 0))
                    if yesterday > 0:
                        dg = (lv / yesterday - 1) * 100

            # 月涨幅
            if month_rows and len(month_rows) >= 2:
                m1 = float(month_rows[0].get("last", 0))
                m0 = float(month_rows[1].get("last", 0))
                if m0 > 0:
                    mg = (m1 / m0 - 1) * 100

            # 周涨幅
            if week_rows and len(week_rows) >= 2:
                w1 = float(week_rows[0].get("last", 0))
                w0 = float(week_rows[1].get("last", 0))
                if w0 > 0:
                    wg = (w1 / w0 - 1) * 100

            result[sym] = {
                "latest_value": round(lv, 2),
                "daily_gain": round(dg, 2),
                "month_gain": round(mg, 2),
                "week_gain": round(wg, 2),
            }
            print(f"  {name}: {lv:.2f} | 日{dg:+.2f}% | 周{wg:+.2f}% | 月{mg:+.2f}%")
        except Exception as e:
            print(f"  {name}: 失败 ({e})")
            result[sym] = {"latest_value": 0, "daily_gain": 0, "month_gain": 0, "week_gain": 0}

    return result


def _parse_md_table(output: str):
    """解析 WeStock CLI 输出的 markdown 表格"""
    lines = output.strip().split("\n")
    data = []
    headers = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if "| --- |" in line:
            continue
        if line.startswith("|"):
            parts = [p.strip() for p in line.split("|")[1:-1]]
            if not headers:
                headers = parts  # 第一个 | 行是表头
            else:
                row = {}
                for i, h in enumerate(headers):
                    row[h] = parts[i] if i < len(parts) else ""
                data.append(row)
    return data


def get_baidu_concepts(code):
    """百度股市通概念板块"""
    try:
        url = f"https://finance.pae.baidu.com/api/getrelatedblock?code={code}&market=ab&typeCode=all&finClientType=pc"
        headers = {
            "User-Agent": UA,
            "Accept": "application/vnd.finance-web.v1+json",
            "Origin": "https://gushitong.baidu.com",
            "Referer": "https://gushitong.baidu.com/",
        }
        s = requests.Session()
        s.trust_env = False
        r = s.get(url, headers=headers, timeout=10)
        d = r.json()
        concepts = []
        if str(d.get("ResultCode", -1)) == "0":
            for block in d.get("Result", []):
                for item in block.get("list", []):
                    concepts.append(item.get("name", ""))
        return concepts[:6]
    except:
        return []


def calc_gains(klines):
    """从K线计算 日/3日/10日/30日 涨幅"""
    if not klines or len(klines) < 2:
        return {"daily_gain": 0, "gain_3d": 0, "gain_10d": 0, "gain_30d": 0}
    closes = [k["close"] for k in klines]
    latest = closes[-1]
    daily = 0
    if len(closes) >= 2:
        daily = (latest / closes[-2] - 1) * 100 if closes[-2] > 0 else 0
    g3 = 0
    if len(closes) >= 4:
        g3 = (latest / closes[-4] - 1) * 100 if closes[-4] > 0 else 0
    g10 = 0
    n10 = min(10, len(closes) - 1)
    if n10 > 0:
        g10 = (latest / closes[-(n10+1)] - 1) * 100 if closes[-(n10+1)] > 0 else 0
    g30 = 0
    n30 = min(30, len(closes) - 1)
    if n30 > 0:
        g30 = (latest / closes[-(n30+1)] - 1) * 100 if closes[-(n30+1)] > 0 else 0
    return {"daily_gain": round(daily, 2), "gain_3d": round(g3, 2),
            "gain_10d": round(g10, 2), "gain_30d": round(g30, 2)}


def calc_abnormal(gains, code):
    """距异动/严重异动距离"""
    g3, g10, g30 = gains.get("gain_3d", 0), gains.get("gain_10d", 0), gains.get("gain_30d", 0)
    is_kc = str(code).startswith("688") or str(code).startswith("300")
    is_bj = str(code).startswith("8")
    da = 20 - abs(g3)
    if is_kc:
        ds10, ds30 = 50 - abs(g10), 100 - abs(g30)
    elif is_bj:
        ds10, ds30 = 60 - abs(g10), 120 - abs(g30)
    else:
        ds10, ds30 = 100 - abs(g10), 200 - abs(g30)
    return {"dist_abnormal": round(da, 2), "dist_severe": round(ds10, 2)}


def save_daily_data(work_dir, date_str, stocks_data, index_data, meta):
    """保存每日数据为 JSON 文件"""
    data_path = os.path.join(work_dir, DATA_DIR)
    os.makedirs(data_path, exist_ok=True)

    daily = {
        "stocks": stocks_data,
        "indices": index_data,
        "meta": meta,
        "date": date_str,
    }

    file_path = os.path.join(data_path, f"{date_str}.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(daily, f, ensure_ascii=False, separators=(",", ":"))
    print(f"  数据已保存: {DATA_DIR}/{date_str}.json")


def load_historical_data(work_dir, max_days=60):
    """读取所有历史数据 JSON，返回 {date: {stocks, indices, meta}}"""
    data_path = os.path.join(work_dir, DATA_DIR)
    if not os.path.exists(data_path):
        return {}

    history = {}
    for fp in sorted(glob.glob(os.path.join(data_path, "*.json"))):
        fname = os.path.basename(fp)
        date_key = fname.replace(".json", "")
        try:
            with open(fp, "r", encoding="utf-8") as f:
                d = json.load(f)
            history[date_key] = {
                "stocks": d.get("stocks", []),
                "indices": d.get("indices", {}),
                "meta": d.get("meta", {}),
            }
        except:
            pass

    # 最多保留 max_days 天
    if len(history) > max_days:
        keys = sorted(history.keys())[-max_days:]
        history = {k: history[k] for k in keys}

    return history


def cleanup_old_data(work_dir, keep_days=100, trigger_interval=600):
    """清理旧数据文件

    每过 trigger_interval 个交易日（即 data/ 目录中 JSON 文件数量达到其倍数时）
    触发一次清理。清理时保留最近 keep_days 个交易日的数据文件，删除更早的文件，
    同时清理 .top100_cache.json 中超过 keep_days 天的日期键。

    Args:
        work_dir: 工作目录路径
        keep_days: 保留最近多少个交易日的数据，默认 100
        trigger_interval: 触发清理的交易日间隔，默认 600
    """
    data_path = os.path.join(work_dir, DATA_DIR)

    # 目录不存在，跳过清理
    if not os.path.exists(data_path):
        print(f"  [清理] data/ 目录不存在，跳过清理")
        return

    # 统计 JSON 文件数量（即交易日数）
    json_files = glob.glob(os.path.join(data_path, "*.json"))
    count = len(json_files)

    # 文件数少于 keep_days，无需清理
    if count < keep_days:
        print(f"  [清理] 当前交易日数: {count}（少于 {keep_days} 天），跳过清理")
        return

    # 检查是否达到触发条件（文件数量是 trigger_interval 的倍数）
    if count % trigger_interval != 0:
        print(f"  [清理] 当前交易日数: {count}（未达到 {trigger_interval} 的倍数），跳过清理")
        return

    # ── 触发清理 ──
    print(f"  [清理] 触发清理！当前交易日数: {count}（{trigger_interval} 的倍数）")
    print(f"  [清理] 保留策略: 最近 {keep_days} 个交易日")

    # 按文件名（日期 YYYY-MM-DD）排序，字母序等同于日期序
    json_files_sorted = sorted(json_files)

    # 需要保留的文件（最近 keep_days 天）
    keep_files = json_files_sorted[-keep_days:]
    # 需要删除的文件
    delete_files = json_files_sorted[:-keep_days] if count > keep_days else []

    # 清理前打印将被删除的文件列表
    print(f"  [清理] 将删除 {len(delete_files)} 个文件:")
    for fp in delete_files:
        print(f"    - {os.path.basename(fp)}")

    # 物理删除文件（只删除 data/ 目录下的 .json 文件）
    deleted_count = 0
    for fp in delete_files:
        try:
            os.remove(fp)
            deleted_count += 1
        except Exception as e:
            print(f"    [WARN] 删除失败: {os.path.basename(fp)} ({e})")

    # 打印保留的日期范围
    keep_dates = [os.path.basename(fp).replace(".json", "") for fp in keep_files]
    print(f"  [清理] 已删除 {deleted_count} 个文件，保留 {len(keep_files)} 个文件")
    print(f"  [清理] 保留日期范围: {keep_dates[0]} ~ {keep_dates[-1]}")
    print(f"  [清理] 保留的日期: {', '.join(keep_dates)}")

    # ── 清理 .top100_cache.json 中超过 keep_days 天的日期键 ──
    cache_file = os.path.join(work_dir, ".top100_cache.json")
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f)

            cache_keys = sorted(cache.keys())
            if len(cache_keys) > keep_days:
                # 保留最近 keep_days 天的键
                keep_keys = set(cache_keys[-keep_days:])
                removed_cache_keys = [k for k in cache_keys if k not in keep_keys]

                # 打印将被清除的缓存键
                print(f"  [清理] 缓存中将清除 {len(removed_cache_keys)} 个日期键:")
                for k in removed_cache_keys:
                    print(f"    - {k}")

                # 清除旧键并写回
                cache = {k: v for k, v in cache.items() if k in keep_keys}
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False)

                print(f"  [清理] 缓存已更新，保留 {len(cache)} 个日期键")
            else:
                print(f"  [清理] 缓存日期键数: {len(cache_keys)}（少于 {keep_days}，无需清理）")
        except Exception as e:
            print(f"  [清理] 缓存清理失败: {e}")
    else:
        print(f"  [清理] 缓存文件不存在，跳过缓存清理")

    print(f"  [清理] 清理完成")


# ── HTML 模板 (用 {{ }} 替代 { } 以避开冲突) ──
def build_html(history_json, latest_date, gen_time, top100_source="", index_source=""):
    """构建完整 HTML（内嵌历史数据，含移动端适配）"""
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<title>A股成交额 TOP100 | 历史回顾</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:"Microsoft YaHei","PingFang SC",-apple-system,sans-serif;background:#f0f2f5;color:#333;font-size:13px;line-height:1.5}}
.hd{{background:linear-gradient(135deg,#1a1a2e,#16213e,#0f3460);color:#fff;padding:15px 25px;box-shadow:0 2px 10px rgba(0,0,0,.2);position:sticky;top:0;z-index:100}}
.hd h1{{font-size:20px;font-weight:700}}
.hd .sub{{font-size:12px;opacity:.8;display:flex;align-items:center;gap:15px;flex-wrap:wrap;margin-top:3px}}
.hd .bdg{{background:rgba(255,255,255,.15);padding:2px 10px;border-radius:10px;font-size:11px}}
.dn{{display:flex;align-items:center;gap:8px;margin-top:6px;flex-wrap:wrap}}
.dn label{{font-size:12px;opacity:.9}}
.dn input[type="date"]{{padding:3px 8px;border:1px solid rgba(255,255,255,.3);border-radius:4px;font-size:12px;background:rgba(255,255,255,.12);color:#fff;outline:none;cursor:pointer}}
.dn input[type="date"]::-webkit-calendar-picker-indicator{{filter:invert(1)}}
.dn .nb{{display:inline-flex;align-items:center;gap:4px}}
.dn button{{padding:3px 12px;border:1px solid rgba(255,255,255,.3);border-radius:4px;background:rgba(255,255,255,.1);color:#fff;font-size:12px;cursor:pointer;white-space:nowrap;transition:background .2s}}
.dn button:hover{{background:rgba(255,255,255,.25)}}
.dn button:disabled{{opacity:.35;cursor:not-allowed}}
.dn .cd{{font-size:13px;font-weight:600;padding:2px 10px;background:rgba(255,255,255,.15);border-radius:4px}}
.dn .av{{font-size:11px;opacity:.7}}
.sb{{display:flex;gap:12px;padding:12px 25px;background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.06);flex-wrap:wrap}}
.sc{{background:#f8f9fa;border-radius:8px;padding:8px 14px;text-align:center;min-width:90px;border:1px solid #e9ecef;flex:1}}
.sc .v{{font-size:20px;font-weight:700}}
.sc .l{{font-size:11px;color:#888;margin-top:2px}}
.sc.r .v{{color:#e74c3c}}
.sc.g .v{{color:#27ae60}}
.sc.t .v{{color:#2c3e50}}
.dsr{{font-size:11px;color:#888;padding:8px 25px 4px;border-bottom:1px solid #f0f0f0;background:#fafbfc}}
.dsr b{{color:#555;font-weight:600}}
.ip{{display:flex;gap:10px;padding:10px 25px;background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.06);flex-wrap:wrap}}
.ic{{background:#f8f9fa;border-radius:8px;padding:8px 12px;min-width:130px;border:2px solid transparent;flex:1;max-width:200px}}
.ic .n{{font-size:11px;color:#666;font-weight:500}}
.ic .v{{font-size:17px;font-weight:700}}
.ic .chg{{font-size:11px;margin:2px 0}}
.ic .rw{{display:flex;justify-content:space-between;align-items:center;font-size:10px;padding:2px 6px;border-radius:3px;margin-top:2px}}
.ic .rw.up{{background:#ffe0e0;color:#e74c3c}}
.ic .rw.sh{{background:#fff8e1;color:#f57f17}}
.ic .rw.dn{{background:#e0f5e9;color:#27ae60}}
.c-up{{color:#e74c3c}}
.c-dn{{color:#27ae60}}
.tc{{margin:0 25px 20px;background:#fff;border-radius:0 0 8px 8px;box-shadow:0 2px 8px rgba(0,0,0,.08);overflow-x:auto}}
.tb{{display:flex;justify-content:space-between;align-items:center;padding:8px 15px;background:#fafafa;border-bottom:1px solid #e9ecef}}
.tb .info{{font-size:12px;color:#888}}
.tb input{{padding:5px 10px;border:1px solid #ddd;border-radius:4px;font-size:12px;width:200px}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{background:#f5f6fa;padding:8px 5px;text-align:center;font-weight:600;color:#555;border-bottom:2px solid #e0e0e0;white-space:nowrap;position:sticky;top:0;z-index:40}}
th.st{{cursor:pointer;user-select:none}}
th.st:hover{{background:#ebeef5}}
th.sd{{color:#e74c3c}}
th.sd::after{{content:" ▼";font-size:10px}}
th.sd.asc::after{{content:" ▲"}}
td{{padding:6px 5px;text-align:center;border-bottom:1px solid #f0f0f0;white-space:nowrap}}
tr:hover{{background:#f0f7ff}}
tr:nth-child(even){{background:#fafbfc}}
tr:nth-child(even):hover{{background:#f0f7ff}}
.rk{{font-weight:700}}
.rk.t3{{color:#e74c3c;font-size:14px}}
.rk.t10{{color:#f39c12}}
.sn{{font-weight:600;color:#2c3e50}}
.scd{{color:#888;font-size:11px}}
.itg{{display:inline-block;background:#e8f0fe;color:#1967d2;padding:2px 6px;border-radius:3px;font-size:10px;margin:1px;max-width:85px;overflow:hidden;text-overflow:ellipsis}}
.gu{{color:#e74c3c}}
.gd{{color:#27ae60}}
.amt{{font-family:Consolas,"Courier New",monospace;font-weight:500}}
.ab{{display:inline-block;height:4px;background:#e74c3c;border-radius:2px;vertical-align:middle;margin-left:2px}}
.gc{{font-weight:600;font-size:12px}}
.ak{{color:#27ae60}}
.an{{font-weight:700;background:#fff3cd;border-radius:3px;padding:1px 3px}}
.at{{color:#e74c3c;font-weight:700;background:#ffe0e0;border-radius:3px;padding:1px 3px}}
.ft{{text-align:center;padding:20px;color:#aaa;font-size:11px}}
.nd{{text-align:center;padding:40px;color:#999}}
.nodata{{text-align:center;padding:60px 20px;color:#999;font-size:15px}}
.nodata .icon{{font-size:40px;margin-bottom:10px}}
.sp{{border:3px solid #f3f3f3;border-top:3px solid #3498db;border-radius:50%;width:30px;height:30px;animation:sp 1s linear infinite;margin:0 auto 10px}}
@keyframes sp{{0%{{transform:rotate(0)}}100%{{transform:rotate(360deg)}}}}

/* ── 移动端卡片布局 ── */
.mc{{display:none}}
.mc .ci{{background:#fff;border-radius:10px;margin:6px 10px;box-shadow:0 1px 4px rgba(0,0,0,.07);overflow:hidden;border:1px solid #eee}}
.mc .ch{{display:flex;align-items:center;padding:10px 12px;gap:8px;cursor:pointer;-webkit-tap-highlight-color:transparent;transition:background .15s}}
.mc .ch:active{{background:#f5f5f5}}
.mc .crk{{min-width:28px;font-weight:700;font-size:14px;text-align:center}}
.mc .crk.t3{{color:#e74c3c}}
.mc .crk.t10{{color:#f39c12}}
.mc .cnm{{flex:1;min-width:0}}
.mc .cnm .nm{{font-weight:600;font-size:14px;color:#2c3e50;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.mc .cnm .cd2{{font-size:11px;color:#999}}
.mc .camt{{text-align:right;min-width:65px}}
.mc .camt .av2{{font-size:13px;font-weight:600;font-family:Consolas,monospace}}
.mc .camt .al2{{font-size:10px;color:#999}}
.mc .cdg{{min-width:60px;text-align:right;font-weight:600;font-size:14px}}
.mc .cdg.up{{color:#e74c3c}}
.mc .cdg.dn{{color:#27ae60}}
.mc .chev{{color:#ccc;font-size:12px;transition:transform .2s;margin-left:2px}}
.mc .ci.exp .chev{{transform:rotate(90deg)}}
.mc .cd3{{display:none;padding:0 12px 10px;border-top:1px solid #f0f0f0;background:#fafbfc}}
.mc .ci.exp .cd3{{display:block}}
.mc .dr{{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #f0f0f0;font-size:12px}}
.mc .dr:last-child{{border-bottom:none}}
.mc .dr .dl{{color:#888}}
.mc .dr .dv{{font-weight:500}}
.mc .tag{{display:inline-block;font-size:10px;padding:1px 5px;border-radius:3px;margin:1px}}
.mc .tag.ind{{background:#e8f0fe;color:#1967d2}}
.mc .tag.abn{{background:#fff3cd;color:#856404}}
.mc .tag.sev{{background:#ffe0e0;color:#e74c3c}}
.mc .tags{{padding:4px 0 2px}}
.new-tag{{color:#42a5f5;font-weight:600}}
tr.row-hot{{background:#fffde7!important}}
.mc .ci-hot{{background:#fffde7!important}}
.risk-bar{{display:flex;align-items:center;gap:12px;padding:8px 15px;margin:8px 25px 0;border-radius:6px;border:2px solid;font-size:12px;flex-wrap:wrap;transition:all .3s}}
.rb-icon{{font-size:16px}}
.rb-phase{{font-weight:700;font-size:14px}}
.rb-item{{color:#555}}
.rb-item b{{color:#333}}
.rb-rule{{color:#777;font-size:11px;flex:1;min-width:200px}}
.market-panel{{margin:0 25px 8px;background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.06);overflow:hidden}}
.mp-title{{padding:8px 15px;background:#f5f6fa;font-weight:600;font-size:13px;border-bottom:1px solid #e9ecef}}
.mp-grid{{display:flex;flex-wrap:wrap;padding:10px 15px;gap:15px}}
.mp-cell{{min-width:120px}}
.mp-label{{font-size:11px;color:#888}}
.mp-val{{font-size:14px;font-weight:600;margin-top:2px}}
.mp-rules{{border-top:1px solid #f0f0f0}}
.mp-rules summary{{padding:8px 15px;cursor:pointer;font-size:12px;color:#555;font-weight:500;user-select:none}}
.mp-rules summary:hover{{background:#f9f9f9}}
.mp-rule-list{{padding:4px 15px 10px}}
.mp-rule-item{{padding:3px 0;font-size:11px;color:#666;line-height:1.6}}
.mp-rule-item b{{color:#444}}

/* ── 移动端响应式 ── */
@media(max-width:768px){{
  .hd{{padding:10px 12px}}
  .hd h1{{font-size:16px}}
  .hd .sub{{gap:8px;margin-top:2px}}
  .hd .bdg{{font-size:10px;padding:1px 8px}}
  .dn{{gap:4px;margin-top:4px}}
  .dn label{{font-size:11px}}
  .dn button{{padding:2px 8px;font-size:11px}}
  .dn input[type="date"]{{padding:2px 6px;font-size:11px;width:130px}}
  .dn .cd{{font-size:11px;padding:1px 6px}}
  .dn .av{{font-size:9px;width:100%;text-align:center;margin-top:2px}}
  .sb{{padding:8px;gap:6px;display:grid;grid-template-columns:1fr 1fr}}
  .sc{{min-width:auto;padding:6px 8px}}
  .sc .v{{font-size:16px}}
  .sc .l{{font-size:10px}}
  .ip{{padding:6px;gap:4px;display:grid;grid-template-columns:1fr 1fr}}
  .ic{{min-width:auto;max-width:none;padding:6px 8px}}
  .ic .v{{font-size:15px}}
  .ic .n{{font-size:10px}}
  .tc{{margin:0 0 10px;border-radius:0}}
  .tb{{padding:6px 8px;flex-wrap:wrap;gap:6px}}
  .tb .info{{display:none}}
  .tb input{{width:100%;font-size:13px;padding:8px 10px}}
  /* 隐藏PC表格，显示移动端卡片 */
  .tc table{{display:none}}
  .mc{{display:block}}
  .ft{{padding:12px 8px;font-size:10px}}
  .dsr{{padding:6px 12px 2px;font-size:10px}}
  .risk-bar{{margin:8px 10px 0;padding:6px 10px;gap:8px;font-size:11px}}
  .rb-rule{{display:none}}
  .market-panel{{margin:0 10px 8px}}
  .mp-grid{{gap:10px}}
  .mp-cell{{min-width:80px;flex:1}}
  .mp-val{{font-size:12px}}
}}
</style>
</head>
<body>
<div class="hd">
<h1>A股市场 · 成交额 TOP100 股票</h1>
<div class="sub">
<span class="bdg" id="ut">加载中...</span>
<span>共 <b id="tc">-</b> 只</span>
</div>
<div class="dn">
<label for="dp">📅</label>
<div class="nb">
<button id="pb" onclick="pd()" title="上一交易日">◀</button>
</div>
<input type="date" id="dp" onchange="cd(this.value)">
<div class="nb">
<button id="nb2" onclick="nd2()" title="下一交易日">▶</button>
</div>
<span class="cd" id="cdsp">-</span>
<span class="av" id="avl"></span>
</div>
</div>
<div class="sb">
<div class="sc r"><div class="v" id="rc">-</div><div class="l">🔴 红盘</div></div>
<div class="sc g"><div class="v" id="gc">-</div><div class="l">🟢 绿盘</div></div>
<div class="sc t"><div class="v" id="ta">-</div><div class="l">💰 TOP100总成交额(亿)</div></div>
<div class="sc t"><div class="v" id="th">-</div><div class="l">📊 第100名门槛(亿)</div></div>
</div>
<div class="risk-bar" id="rb"></div>
<div class="dsr" id="dsri">📊 主要指数 · <b>数据来源: {index_source or "无"}</b></div>
<div class="ip" id="ip"></div>
<div class="market-panel" id="mp"></div>
<div class="dsr" id="dsrt">💰 TOP100排行 · <b>数据来源: {top100_source or "无"}</b></div>
<div class="tc">
<div class="tb">
<span class="info">点击表头排序 | 搜索过滤</span>
<input type="text" id="si" placeholder="🔍 搜索股票/代码..." oninput="df()">
</div>
<table>
<thead>
<tr>
<th width="36">#</th>
<th width="80">名称</th>
<th width="60">代码</th>
<th width="105">行业/概念</th>
<th width="68" class="st" onclick="ds('price')">收盘价</th>
<th width="115" class="st" onclick="ds('amount')">成交额(亿) ▼</th>
<th width="68" class="st" onclick="ds('daily_gain')">当日涨幅</th>
<th width="62" class="st" onclick="ds('gain_3d')">3日涨幅</th>
<th width="65" class="st" onclick="ds('gain_10d')">10日涨幅</th>
<th width="65" class="st" onclick="ds('gain_30d')">30日涨幅</th>
<th width="75" class="st" onclick="ds('dist_abnormal')">距异动</th>
<th width="80" class="st" onclick="ds('dist_severe')">距严重异动</th>
<th width="55">连续上榜</th>
<th width="50">10日上榜</th>
<th width="50">30日上榜</th>
</tr>
</thead>
<tbody id="tb"><tr class="nd"><td colspan="15"><div class="sp"></div>加载中...</td></tr></tbody>
</table>
<!-- 移动端卡片容器 -->
<div class="mc" id="mc"><div style="text-align:center;padding:40px 20px;color:#999"><div class="sp"></div>加载中...</div></div>
</div>
<div class="ft">
<p>⚠️ 以上信息源自第三方数据整理，仅供参考，不构成投资建议。</p>
<p>数据来源: 东方财富 · 通达信 · 腾讯财经 · 百度股市通 · 沪深交易所公开数据</p>
<p>异动规则: 连续3日涨幅偏离值±20% | 严重异动: 科创/创业板10日±50%/30日±100%, 主板10日±100%/30日±200%</p>
<p>生成时间: <span id="gt">{gen_time}</span></p>
</div>
<script>
var H={history_json};
var DK=Object.keys(H).sort();/* 所有可用日期, 升序 */
var LD="{latest_date}";/* 最新日期 */
var CD=LD;/* 当前显示日期 */
var sf="amount",sa=false;
var ix={{sh000001:{{n:"上证指数"}},sz399001:{{n:"深证成指"}},sz399006:{{n:"创业板指"}},sh000688:{{n:"科创50"}},sz399005:{{n:"中小100"}},sh000300:{{n:"沪深300"}}}};

/* ── 工具函数 ── */
function fp(v){{if(v==null)return"-";var s=v>0?"+":"";return s+v.toFixed(2)+"%"}}
function fg(v){{if(v==null)return'<span class="gc">-</span>';var c=v>0?"gu":(v<0?"gd":""),s=v>0?"+":"";return'<span class="gc '+c+'">'+s+v.toFixed(2)+"%</span>"}}
function fn(v){{if(v==null)return"-";if(v<0)return'<span class="at">触发!</span>';if(v<8)return'<span class="an">距 '+v.toFixed(1)+"%</span>";return'<span class="ak">'+v.toFixed(1)+"%</span>"}}
function fb(y,my){{var yi=y/1e8,p=my>0?(yi/my*100):0;return yi.toFixed(2)+'<span class="ab" style="width:'+Math.max(p,1)+'px"></span>'}}
function es(s){{return(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}}

/* ── 日期导航 ── */
function cd(d){{/* 选择日期 */
  if(!H[d]){{document.getElementById("tb").innerHTML='<tr><td colspan="15" class="nodata"><div class="icon">📭</div>'+d+' 无数据</td></tr>';document.getElementById("mc").innerHTML='<div class="nodata"><div class="icon">📭</div>'+d+' 无数据</div>';return}}
  CD=d;ld();/* 加载数据 */
}}
function pd(){{/* 上一交易日 */
  var i=DK.indexOf(CD);if(i>0){{CD=DK[i-1];ld()}}
}}
function nd2(){{/* 下一交易日 */
  var i=DK.indexOf(CD);if(i<DK.length-1){{CD=DK[i+1];ld()}}
}}
function ld(){{/* 加载当前日期数据 */
  var day=H[CD];if(!day)return;
  var D=day.stocks||[],I=day.indices||{{}},M=day.meta||{{}};
  /* 更新日期选择器 */
  document.getElementById("dp").value=CD;
  document.getElementById("cdsp").textContent=CD;
  document.getElementById("ut").textContent=CD;
  document.getElementById("tc").textContent=D.length;
  document.getElementById("gt").textContent=M.generate_time||"";
  /* 上一日/下一日按钮状态 */
  var ci=DK.indexOf(CD);
  document.getElementById("pb").disabled=(ci<=0);
  document.getElementById("nb2").disabled=(ci>=DK.length-1);
  /* 可用日期范围提示 */
  document.getElementById("avl").textContent="可用数据: "+DK[0]+" ~ "+DK[DK.length-1]+" ("+DK.length+"天)";
  document.getElementById("dp").min=DK[0];
  document.getElementById("dp").max=DK[DK.length-1];
  /* 渲染 */
  rs(D);ri(I);rr(I);rm(I);rt(D);
}}

/* ── 统计栏 ── */
function rs(D){{var r=0,g=0,t=0;D.forEach(function(s){{if(s.daily_gain>0)r++;else if(s.daily_gain<0)g++;t+=s.amount||0}});document.getElementById("rc").textContent=r;document.getElementById("gc").textContent=g;document.getElementById("ta").textContent=(t/1e8).toFixed(0);var th=D.length>0?(D[D.length-1].amount||0)/1e8:0;document.getElementById("th").textContent=th.toFixed(2)}}

/* ── 指数面板 ── */
function ri(I){{var h="";for(var s in ix){{var x=I[s];if(!x)continue;var mg=x.month_gain||0,wg=x.week_gain||0;var mc=mg>5?"up":(mg<-5?"dn":"sh"),wc=wg>5?"up":(wg<-5?"dn":"sh");var ms=mg>5?"📈":(mg<-5?"📉":"📊"),ws=wg>5?"📈":(wg<-5?"📉":"📊");h+='<div class="ic"><div class="n">'+ix[s].n+'</div>';h+='<div class="v">'+(x.latest_value||0).toFixed(2)+'</div>';h+='<div class="chg '+(x.daily_gain>0?"c-up":x.daily_gain<0?"c-dn":"")+'">'+fp(x.daily_gain)+'</div>';h+='<div class="rw '+mc+'"><span>月涨幅 '+ms+'</span><span>'+fp(mg)+'</span></div>';h+='<div class="rw '+wc+'"><span>周涨幅 '+ws+'</span><span>'+fp(wg)+'</span></div>';h+='</div>'}}document.getElementById("ip").innerHTML=h}}

/* ── 风险等级提示条 ── */
function rr(I){{
  var sh=I["sh000001"];if(!sh){{document.getElementById("rb").innerHTML="";return}}
  var mg=sh.month_gain||0;
  var phase,color,icon,target,pos,rule;
  if(mg<-5){{phase="下跌市";color="#e74c3c";icon="🔴";target="+0%（不亏损即完成）";pos="空仓休息";rule="大盘月涨幅<-5%，停止操作，空仓回避主跌段"}}
  else if(mg<-2){{phase="平衡市偏弱";color="#e67e22";icon="🟠";target="+20%/月";pos="≤50%仓位";rule="周阴线-8%或两周连阴，无条件清仓退出"}}
  else if(mg<2){{phase="平衡市";color="#f39c12";icon="🟡";target="+20%/月";pos="半仓操作";rule="周目标+10%，超越大盘涨幅1倍"}}
  else if(mg<5){{phase="平衡市偏强";color="#8bc34a";icon="🟢";target="+20%/月";pos="可适当积极";rule="关注热点龙头，强势股从容低吸"}}
  else{{phase="上涨市";color="#2ecc71";icon="🟢";target="+30%及以上/月";pos="可满仓操作";rule="追求周连阳，复利快速增长"}}
  var rb=document.getElementById("rb");
  rb.innerHTML='<span class="rb-icon">'+icon+'</span><span class="rb-phase" style="color:'+color+'">'+phase+'</span><span class="rb-item">月目标: <b>'+target+'</b></span><span class="rb-item">仓位: <b>'+pos+'</b></span><span class="rb-rule">'+rule+'</span>';
  rb.style.borderColor=color;
  rb.style.background=color+"15";
}}

/* ── 大盘环境判断面板 ── */
function rm(I){{
  var sh=I["sh000001"];if(!sh){{document.getElementById("mp").innerHTML="";return}}
  var mg=sh.month_gain||0,wg=sh.week_gain||0,dg=sh.daily_gain||0;
  var phase,color,icon,target,pos,rule;
  if(mg<-5){{phase="下跌市";color="#e74c3c";icon="🔴";target="+0%（不亏损即完成）";pos="空仓休息";rule="大盘月涨幅<-5%，停止操作，空仓回避主跌段"}}
  else if(mg<-2){{phase="平衡市偏弱";color="#e67e22";icon="🟠";target="+20%/月";pos="≤50%仓位";rule="周阴线-8%或两周连阴，无条件清仓退出"}}
  else if(mg<2){{phase="平衡市";color="#f39c12";icon="🟡";target="+20%/月";pos="半仓操作";rule="周目标+10%，超越大盘涨幅1倍"}}
  else if(mg<5){{phase="平衡市偏强";color="#8bc34a";icon="🟢";target="+20%/月";pos="可适当积极";rule="关注热点龙头，强势股从容低吸"}}
  else{{phase="上涨市";color="#2ecc71";icon="🟢";target="+30%及以上/月";pos="可满仓操作";rule="追求周连阳，复利快速增长"}}
  var h="";
  h+='<div class="mp-title">🎯 好运哥交易系统 · 市场环境判断</div>';
  h+='<div class="mp-grid">';
  h+='<div class="mp-cell"><div class="mp-label">市场阶段</div><div class="mp-val" style="color:'+color+'">'+icon+' '+phase+'</div></div>';
  h+='<div class="mp-cell"><div class="mp-label">月赢利目标</div><div class="mp-val">'+target+'</div></div>';
  h+='<div class="mp-cell"><div class="mp-label">仓位建议</div><div class="mp-val">'+pos+'</div></div>';
  h+='<div class="mp-cell"><div class="mp-label">上证月涨幅</div><div class="mp-val '+(mg>0?"c-up":"c-dn")+'">'+fp(mg)+'</div></div>';
  h+='<div class="mp-cell"><div class="mp-label">上证周涨幅</div><div class="mp-val '+(wg>0?"c-up":"c-dn")+'">'+fp(wg)+'</div></div>';
  h+='</div>';
  h+='<details class="mp-rules"><summary>📋 交易纪律速查（点击展开/收起）</summary>';
  h+='<div class="mp-rule-list">';
  h+='<div class="mp-rule-item"><b>月目标：</b>上涨市+30%，平衡市+20%，下跌市空仓（+0%不亏损即完成）</div>';
  h+='<div class="mp-rule-item"><b>周目标：</b>+10%，超越大盘涨幅1倍为基本目标</div>';
  h+='<div class="mp-rule-item"><b>周阴线-8%或两周连阴：</b>无条件清仓退出，查找原因，重新制定计划</div>';
  h+='<div class="mp-rule-item"><b>日3连阴：</b>无条件退出交易，检查原因</div>';
  h+='<div class="mp-rule-item"><b>日2连阴：</b>高度警惕，检查行情和操作节奏</div>';
  h+='<div class="mp-rule-item"><b>下跌市原则：</b>不追高，涨幅+3%以上坚决不追涨，空仓为第一原则</div>';
  h+='<div class="mp-rule-item"><b>仓位控制：</b>行情良好满仓单一品种，不利时空仓，风险期≤50%</div>';
  h+='<div class="mp-rule-item"><b>强势股操作：</b>只做龙一龙二，预期收益低于+10%不做，买入后操作期1-2周</div>';
  h+='<div class="mp-rule-item"><b>卖点纪律：</b>强势品种放大量收阴须当日清仓，错买第一时间止损</div>';
  h+='<div class="mp-rule-item"><b>心态管理：</b>生活中有影响心态的事件时暂停操作，休息也是战斗</div>';
  h+='</div></details>';
  document.getElementById("mp").innerHTML=h;
}}

/* ── PC表格渲染 ── */
function rt(a){{var tb=document.getElementById("tb");var mc=document.getElementById("mc");if(!a||!a.length){{tb.innerHTML='<tr><td colspan="15" class="nd">暂无数据</td></tr>';mc.innerHTML='<div class="nodata"><div class="icon">📭</div>暂无数据</div>';return}}var my=Math.max.apply(null,a.map(function(s){{return s.amount||0}}));/* PC表格 */var h="";a.forEach(function(s,i){{var rk=i+1,rc=rk<=3?"t3":(rk<=10?"t10":""),gc=s.daily_gain>0?"gu":(s.daily_gain<0?"gd":"");var hot=s.consecutive>=10;h+='<tr'+(hot?' class="row-hot"':'')+'>';h+='<td class="rk '+rc+'">'+rk+'</td>';h+='<td><span class="sn">'+es(s.name)+'</span></td>';h+='<td><span class="scd">'+es(s.code)+'</span></td>';h+='<td><span class="itg">'+es(s.industry||"-")+'</span></td>';h+='<td class="amt">'+(s.price?(s.price<1?s.price.toFixed(3):s.price.toFixed(2)):"-")+'</td>';h+='<td class="amt">'+fb(s.amount||0,my)+'</td>';h+='<td class="'+gc+'">'+fg(s.daily_gain)+'</td>';h+='<td>'+fg(s.gain_3d)+'</td>';h+='<td>'+fg(s.gain_10d)+'</td>';h+='<td>'+fg(s.gain_30d)+'</td>';h+='<td>'+fn(s.dist_abnormal)+'</td>';h+='<td>'+fn(s.dist_severe)+'</td>';h+='<td>'+(s.consecutive===-1?'<span class="new-tag">NEW</span>':s.consecutive>=5?s.consecutive+'🔥':s.consecutive)+'</td>';h+='<td>'+(s.board_10d||0)+'</td>';h+='<td>'+(s.board_30d||0)+'</td>';h+='</tr>'}});tb.innerHTML=h;/* 移动端卡片 */var mh="";a.forEach(function(s,i){{var rk=i+1,rc=rk<=3?"t3":(rk<=10?"t10":"");var dg=s.daily_gain||0;var dc=dg>0?"up":(dg<0?"dn":"");var ds2=dg>0?"+":"";var amtYi=((s.amount||0)/1e8).toFixed(2);var hot2=s.consecutive>=10;mh+='<div class="ci'+(hot2?' ci-hot':'')+'" onclick="tg(this)">';mh+='<div class="ch">';mh+='<div class="crk '+rc+'">'+rk+'</div>';mh+='<div class="cnm"><div class="nm">'+es(s.name)+'</div><div class="cd2">'+es(s.code)+'</div></div>';mh+='<div class="cdg '+dc+'">'+ds2+dg.toFixed(2)+'%</div>';mh+='<div class="camt"><div class="av2">'+amtYi+'亿</div><div class="al2">成交额</div></div>';mh+='<div class="chev">▶</div>';mh+='</div>';/* 展开详情 */mh+='<div class="cd3">';mh+='<div class="dr"><span class="dl">行业/概念</span><span class="dv"><span class="tag ind">'+es(s.industry||"-")+'</span></span></div>';mh+='<div class="dr"><span class="dl">收盘价</span><span class="dv">'+(s.price?(s.price<1?s.price.toFixed(3):s.price.toFixed(2)):"-")+'</span></div>';mh+='<div class="dr"><span class="dl">成交额</span><span class="dv amt">'+amtYi+' 亿</span></div>';mh+='<div class="dr"><span class="dl">3日涨幅</span><span class="dv '+(s.gain_3d>0?"gu":s.gain_3d<0?"gd":"")+'">'+fp(s.gain_3d)+'</span></div>';mh+='<div class="dr"><span class="dl">10日涨幅</span><span class="dv '+(s.gain_10d>0?"gu":s.gain_10d<0?"gd":"")+'">'+fp(s.gain_10d)+'</span></div>';mh+='<div class="dr"><span class="dl">30日涨幅</span><span class="dv '+(s.gain_30d>0?"gu":s.gain_30d<0?"gd":"")+'">'+fp(s.gain_30d)+'</span></div>';var da=s.dist_abnormal,dse=s.dist_severe;var abnTag=da!=null&&da<0?'<span class="tag sev">触发</span>':da!=null&&da<8?'<span class="tag abn">距'+da.toFixed(1)+'%</span>':'';var sevTag=dse!=null&&dse<0?'<span class="tag sev">触发</span>':dse!=null&&dse<8?'<span class="tag abn">距'+dse.toFixed(1)+'%</span>':'';mh+='<div class="dr"><span class="dl">距异动</span><span class="dv tags">'+fn(da)+' '+abnTag+'</span></div>';mh+='<div class="dr"><span class="dl">距严重异动</span><span class="dv tags">'+fn(dse)+' '+sevTag+'</span></div>';mh+='<div class="dr"><span class="dl">连续上榜</span><span class="dv">'+(s.consecutive===-1?'<span class="new-tag">NEW</span>':s.consecutive>=5?s.consecutive+'天🔥':s.consecutive+'天')+'</span></div>';mh+='<div class="dr"><span class="dl">上榜次数</span><span class="dv">10日 '+(s.board_10d||0)+'次 / 30日 '+(s.board_30d||0)+'次</span></div>';mh+='</div></div>'}});mc.innerHTML=mh}}

/* ── 卡片展开/折叠 ── */
function tg(el){{el.classList.toggle("exp")}}

/* ── 排序 ── */
function ds(f){{if(sf===f)sa=!sa;else{{sf=f;sa=false}}var day=H[CD];if(!day)return;var a=(day.stocks||[]).slice();a.sort(function(x,y){{var vx=x[f]||0,vy=y[f]||0;return sa?(vx-vy):(vy-vx)}});document.querySelectorAll("th.sd,th.sd.asc").forEach(function(x){{x.className=x.className.replace(/\\s*sd|\\s*asc/g,"").trim()}});var th=document.querySelector("th[onclick*='"+f+"']");if(th){{th.classList.add("sd");if(sa)th.classList.add("asc")}}rt(a)}}

/* ── 搜索过滤 ── */
function df(){{var q=document.getElementById("si").value.toLowerCase().trim();var day=H[CD];if(!day)return;var D=day.stocks||[];if(!q){{rt(D);return}}var f=D.filter(function(s){{return(s.name||"").toLowerCase().indexOf(q)>=0||(s.code||"").indexOf(q)>=0||(s.industry||"").toLowerCase().indexOf(q)>=0}});rt(f)}}

/* ── 初始化 ── */
document.addEventListener("DOMContentLoaded",function(){{ld()}});
</script>
</body>
</html>'''


def main():
    print("=" * 60)
    print("  A股成交额 TOP100 数据报告生成器 v4")
    print("=" * 60)

    t0 = time.time()
    # 统一使用北京时间 (UTC+8)，确保本地和 GitHub Actions 显示一致
    beijing_tz = timezone(timedelta(hours=8))
    now = datetime.now(beijing_tz)
    date_str = now.strftime("%Y-%m-%d")
    gen_time = now.strftime("%Y-%m-%d %H:%M:%S")
    work_dir = os.path.dirname(os.path.abspath(__file__))

    # ── 1. TOP100 ──
    print("\n[1/6] 获取成交额 TOP100（东财）...")
    items, total, top100_source = get_top100()
    print(f"  获取 {len(items)} 条 (全市场 {total} 只) [来源: {top100_source or '无'}]")

    if not items:
        print("[ERROR] 东财数据获取失败（3次重试均失败），尝试从历史数据生成HTML...")
        history = load_historical_data(work_dir, max_days=60)
        if not history:
            print("[ERROR] 无历史数据，无法生成报告")
            sys.exit(1)
        latest_date = sorted(history.keys())[-1]
        slim_history = {}
        for dk, dv in history.items():
            m = dv.get("meta", {})
            slim_history[dk] = {
                "stocks": dv.get("stocks", []),
                "indices": dv.get("indices", {}),
                "meta": {"date": m.get("date", dk), "generate_time": m.get("generate_time", ""), "total": m.get("total", 0)},
            }
        html = build_html(
            json.dumps(slim_history, ensure_ascii=False, separators=(",", ":")),
            latest_date, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "无（历史数据）", "无（历史数据）"
        )
        output_path = os.path.join(work_dir, OUTPUT_FILE)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\n  报告已从历史数据生成: {OUTPUT_FILE}")
        print(f"  历史天数: {len(slim_history)}")
        print("[ERROR] 数据未更新！今日数据获取失败，HTML仅含历史数据（时间戳已更新）")
        print("[ERROR] 请检查东财API连通性或重试")
        sys.exit(1)

    stocks = []
    for it in items[:100]:
        c = str(it.get("f12", "")).zfill(6)
        n = it.get("f14", "")
        if not c or not n:
            continue
        stocks.append({
            "code": c, "name": n,
            "amount": it.get("f6", 0),
            "daily_gain": round(float(it.get("f3", 0)), 2),
            "industry": it.get("f100", ""),
            "price": it.get("f2", 0),
            "mcap": it.get("f20", 0),
        })
    print(f"  有效: {len(stocks)} 只")

    # ── 2. K线 ──
    print(f"\n[2/6] 获取 50 日 K 线（mootdx TCP, {len(stocks)} 只）...")
    ok = 0
    for i, s in enumerate(stocks):
        kls = get_mootdx_klines(s["code"], 50)
        if kls:
            g = calc_gains(kls)
            s.update(g)
            ok += 1
        else:
            s["gain_3d"] = 0
            s["gain_10d"] = 0
            s["gain_30d"] = 0
        if (i + 1) % 20 == 0:
            print(f"  进度: {i+1}/{len(stocks)}")
    print(f"  成功: {ok}/{len(stocks)}")

    # ── 3. 指数 ──
    print("\n[3/6] 获取主要指数（WeStock Data）...")
    index_data = get_index_data()
    index_source = "WeStock Data" if index_data else "无数据"
    print(f"  获取 {len(index_data)} 个指数 [来源: {index_source}]")

    # ── 4. 异动 ──
    print("\n[4/6] 计算异动/严重异动距离...")
    for s in stocks:
        g = {"gain_3d": s.get("gain_3d", 0),
             "gain_10d": s.get("gain_10d", 0),
             "gain_30d": s.get("gain_30d", 0)}
        ab = calc_abnormal(g, s["code"])
        s["dist_abnormal"] = ab["dist_abnormal"]
        s["dist_severe"] = ab["dist_severe"]

    # ── 5. 概念 ──
    print("\n[5/6] 获取概念板块（百度股市通, 前30名）...")
    for i, s in enumerate(stocks[:30]):
        cs = get_baidu_concepts(s["code"])
        s["concepts"] = cs
        s["industry_detail"] = (s.get("industry", "") + " | " + ",".join(cs[:3])).strip(" | ") if cs else s.get("industry", "")
        time.sleep(0.3)
        if (i + 1) % 10 == 0:
            print(f"  进度: {i+1}/30")
    for s in stocks[30:]:
        s["concepts"] = []
        s["industry_detail"] = s.get("industry", "")

    # ── 6. 上榜次数 ──
    print("\n[6/6] 计算上榜次数（缓存 + data/ 回补）...")
    cache_file = os.path.join(work_dir, ".top100_cache.json")

    # 6.1 加载缓存（日期 → 当日上榜股票代码列表）
    cache = {}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            print("  缓存读取失败，从空缓存开始")
            cache = {}
    cache_days = len(cache)

    # 6.2 扫描 data/ 目录，从历史 JSON 补充缓存中缺失的日期
    #     缓存已有的日期以缓存为准；缓存没有的日期从 data/*.json 补充
    data_path = os.path.join(work_dir, DATA_DIR)
    data_supplement_days = 0
    if os.path.isdir(data_path):
        for fp in glob.glob(os.path.join(data_path, "*.json")):
            dk = os.path.basename(fp).replace(".json", "")
            if dk == date_str:
                continue  # 排除今天（今天尚未计入历史）
            if dk in cache:
                continue  # 缓存已有，以缓存为准
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    d = json.load(f)
                codes = [str(s.get("code", "")).zfill(6)
                         for s in d.get("stocks", []) if s.get("code")]
                if codes:
                    cache[dk] = codes
                    data_supplement_days += 1
            except Exception:
                continue  # 跳过损坏/格式异常的文件

    # 6.3 按日期降序取最近 30 天（排除今天），构建历史上榜集合
    hist_dates = sorted(
        [dk for dk in cache.keys() if dk != date_str],
        reverse=True
    )[:30]
    hist = [set(cache[dk]) for dk in hist_dates]
    print(f"  历史数据: {len(hist)} 天（缓存 {cache_days} 天 + data/ 补充 {data_supplement_days} 天）")

    # 6.4 计算每只股票近 10 日 / 近 30 日上榜次数
    for s in stocks:
        a10, a30 = 0, 0
        for j, hset in enumerate(hist):
            if s["code"] in hset:
                if j < 10:
                    a10 += 1
                a30 += 1
        s["board_10d"] = a10
        s["board_30d"] = a30

    # 6.4.1 计算连续上榜天数（从今天往回数，连续在榜的最大天数）
    # hist 是降序排列的历史代码集合列表（昨天、前天、更前天...）
    today_codes = set(s["code"] for s in stocks)
    yesterday_codes = hist[0] if hist else set()  # 最近一个交易日（昨天）

    for s in stocks:
        if s["code"] not in yesterday_codes:
            s["consecutive"] = -1  # -1 表示今天新上榜，前端显示 NEW
        else:
            consec = 1  # 今天上榜算1天
            for hset in hist:  # hist 是降序的（昨天、前天、更前天...）
                if s["code"] in hset:
                    consec += 1
                else:
                    break  # 一旦断开就停止计数
            s["consecutive"] = consec

    # 6.5 更新缓存：写入今日代码 + 保留 data/ 回补的历史日期，一劳永逸
    cache[date_str] = [s["code"] for s in stocks]
    cache = dict(sorted(cache.items())[-60:])
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    print(f"  缓存已更新（今日 + 回补历史，共 {len(cache)} 天）")

    # ── 生成数据 ──
    stocks_data = [{
        "name": s["name"], "code": s["code"],
        "industry": s.get("industry_detail", s.get("industry", "-")),
        "price": s.get("price", 0),
        "amount": s["amount"],
        "daily_gain": s.get("daily_gain", 0),
        "gain_3d": s.get("gain_3d", 0),
        "gain_10d": s.get("gain_10d", 0),
        "gain_30d": s.get("gain_30d", 0),
        "dist_abnormal": s.get("dist_abnormal", 0),
        "dist_severe": s.get("dist_severe", 0),
        "consecutive": s.get("consecutive", 0),
        "board_10d": s.get("board_10d", 0),
        "board_30d": s.get("board_30d", 0),
    } for s in stocks]

    meta = {"date": date_str, "generate_time": gen_time, "total": len(stocks_data)}

    # ── 保存当日 JSON ──
    print("\n保存当日数据...")
    save_daily_data(work_dir, date_str, stocks_data, index_data, meta)

    # ── 清理旧数据（每 600 个交易日触发一次，保留最近 100 天）──
    cleanup_old_data(work_dir, keep_days=100, trigger_interval=600)

    # ── 加载历史数据 ──
    print("加载历史数据...")
    history = load_historical_data(work_dir, max_days=60)
    print(f"  可用天数: {len(history)}")
    latest_date = sorted(history.keys())[-1] if history else date_str

    # ── 生成 HTML ──
    print("\n生成 HTML...")
    # 为减小体积，历史数据中每个日期只保留核心字段
    slim_history = {}
    for dk, dv in history.items():
        m = dv.get("meta", {})
        slim_history[dk] = {
            "stocks": dv.get("stocks", []),
            "indices": dv.get("indices", {}),
            "meta": {"date": m.get("date", dk), "generate_time": m.get("generate_time", ""), "total": m.get("total", 0)},
        }

    html = build_html(
        json.dumps(slim_history, ensure_ascii=False, separators=(",", ":")),
        latest_date, gen_time,
        top100_source or "无", index_source
    )
    output_path = os.path.join(work_dir, OUTPUT_FILE)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    elapsed = time.time() - t0
    html_size = os.path.getsize(output_path)
    print(f"\n  报告已生成: {OUTPUT_FILE} ({html_size/1024:.0f} KB)")
    print(f"  ⏱  总耗时: {elapsed:.1f} 秒")
    print(f"  📊  {len(stocks_data)} 股票 + {len(index_data)} 指数 + {len(slim_history)} 天历史")


if __name__ == "__main__":
    main()
