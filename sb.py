#!/usr/bin/env python3
"""Tarver Scoreboard data script (nfl, ncaafb, mlb, ufc, nascar, f1, moto).

Usage:
  python3 sb.py tick --db DIR --out DIR [--sports nfl,ncaafb,mlb] [--now ISO]
    DIR holds the app docs as <doc_id>.json (scores collection) plus favorites.json.
    Favorites' games are always refreshed. Leagues named in --sports are refreshed in full.
    Writes changed docs to OUT/<doc_id>.json and a summary to OUT/report.json.
The script never invents data: a game it cannot read keeps its stored copy.
"""
import argparse, datetime as dt, json, os, re, sys, time, urllib.request
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
VERSION = "1.3"
PATHS = {"nfl": "football/nfl", "ncaafb": "football/college-football", "mlb": "baseball/mlb"}
ABBR = {  # ESPN -> app
    "nfl": {"JAX": "JAC", "LAR": "LA", "WSH": "WAS"},
    "ncaafb": {"OU": "OKLA", "MIZ": "MIZZ", "SC": "SCAR", "TA&M": "TXAM", "IU": "IND", "BOIS": "BSU", "NU": "NW"},
    "mlb": {"CHW": "CWS", "ARI": "AZ"},
}
MLB_IDS = {"ATL": 144}

# ---------------------------------------------------------------- helpers

NO_TV = ("tv", "watch")
LEARNED = {}

def strip_tv(o):
    if isinstance(o, dict):
        return {k: strip_tv(v) for k, v in o.items() if k not in NO_TV}
    if isinstance(o, list):
        return [strip_tv(v) for v in o]
    return o

def dump(doc, path):
    """Every app doc is written through here: Aaron does not want TV networks shown, so they are never saved."""
    json.dump(strip_tv(doc), open(path, "w"), ensure_ascii=False)

def load_aliases(db):
    """Team spellings learned on earlier runs (ESPN abbreviation -> the app's), stored in ops/aliases."""
    p = os.path.join(db, "aliases.json")
    if os.path.exists(p):
        j = json.load(open(p))
        j = j.get("data", j) if isinstance(j, dict) else {}
        for lg, m in (j.get("map") or {}).items():
            if lg in ABBR:
                for k, v in m.items():
                    ABBR[lg].setdefault(k, v)

def save_aliases(db, out, rep):
    if not LEARNED:
        return
    p = os.path.join(db, "aliases.json")
    j = json.load(open(p)) if os.path.exists(p) else {}
    j = j.get("data", j) if isinstance(j, dict) else {}
    m = j.get("map") or {}
    for lg, d in LEARNED.items():
        m.setdefault(lg, {}).update(d)
    json.dump({"map": m, "note": "ESPN team abbreviations mapped to the app's, learned when a game matched by kickoff time.",
               "updatedAt": iso(dt.datetime.now(UTC))}, open(os.path.join(out, "aliases.json"), "w"), ensure_ascii=False)
    rep["opsDocs"] = ["aliases"]
    rep["warnings"].append("learned team spellings: %s" % LEARNED)
def fetch(url, tries=3):
    if "espn.com" in url:
        url += ("&" if "?" in url else "?") + "cb=%d" % time.time()
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa
            err = e
            time.sleep(4 * (i + 1))
    raise RuntimeError("fetch failed: %s (%s)" % (url.split("?")[0], err))

def ts(s):
    if not s:
        return None
    d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)

def iso(d):
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return int(f) if f == int(f) else f

def short(name):
    p = (name or "").strip().split()
    if len(p) < 2:
        return name
    return p[0][0] + ". " + " ".join(p[1:])

def load(db, doc):
    p = os.path.join(db, doc + ".json")
    if not os.path.exists(p):
        return None
    j = json.load(open(p))
    return j["data"] if isinstance(j, dict) and "data" in j and "id" in j else j

def favorites(db):
    f = load(db, "favorites") or {}
    out = {}
    for t in f.get("teams", []):
        lg, _, key = t.partition(":")
        out.setdefault(lg, set()).add(key)
    return out

# ---------------------------------------------------------------- ESPN scoreboard
def status_of(lg, st):
    t = st["type"]; name = t.get("name", ""); state = t.get("state")
    if name in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_SUSPENDED", "STATUS_FORFEIT"):
        return "ppd", t.get("shortDetail") or t.get("description")
    if name in ("STATUS_DELAYED", "STATUS_RAIN_DELAY"):
        return "live", "Delayed"
    if state == "pre":
        return "pre", None
    p = st.get("period") or 0
    if state == "post":
        if lg == "mlb":
            return "final", t.get("shortDetail") or "Final"
        return "final", "Final/OT" if p > 4 else "Final"
    if lg == "mlb":
        return "live", t.get("shortDetail")
    if name == "STATUS_HALFTIME":
        return "live", "Halftime"
    q = "OT" if p > 4 else "Q%d" % p
    if p > 5:
        q = "%dOT" % (p - 4)
    if name == "STATUS_END_PERIOD":
        return "live", "End " + q
    return "live", "%s · %s" % (q, st.get("displayClock", ""))

def parse_events(lg, sb):
    out = []
    for e in sb.get("events", []):
        c = e["competitions"][0]
        side = {}
        for t in c["competitors"]:
            ab = ABBR[lg].get(t["team"]["abbreviation"], t["team"]["abbreviation"])
            side[t["homeAway"]] = {
                "abbr": ab, "name": t["team"].get("displayName"),
                "score": num(t.get("score")) if t.get("score") not in (None, "") else None,
                "rank": (t.get("curatedRank") or {}).get("current"),
                "lines": [num(l.get("value", l.get("displayValue"))) for l in t.get("linescores", [])],
            }
        status, detail = status_of(lg, c.get("status") or e["status"])
        tv = ", ".join(n for b in c.get("broadcasts", []) for n in b.get("names", []))
        v = c.get("venue") or {}
        addr = v.get("address") or {}
        venue = ", ".join(x for x in [v.get("fullName"), addr.get("city"), addr.get("state")] if x)
        label = None
        for n_ in c.get("notes", []) or []:
            m_ = re.match(r"^(ALWC|NLWC|ALDS|NLDS|ALCS|NLCS|World Series)\s*-\s*Game\s*(\d+)", n_.get("headline", ""))
            if m_:
                label = "%s G%s" % ("WS" if m_[1] == "World Series" else m_[1], m_[2])
        out.append({"eid": e["id"], "start": ts(e["date"]), "status": status, "detail": detail,
                    "away": side["away"], "home": side["home"], "tv": tv, "venue": venue, "label": label})
    return out

def scoreboard(lg, dates):
    from concurrent.futures import ThreadPoolExecutor
    def one(d):
        q = "dates=%s" % d
        if lg == "ncaafb":
            q += "&groups=80&limit=300"
        return parse_events(lg, fetch("https://site.api.espn.com/apis/site/v2/sports/%s/scoreboard?%s" % (PATHS[lg], q)))
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(one, sorted(dates)))
    return [e for r in res for e in r]

def match(game, evs, used, lg=None):
    """Closest ESPN event with the same two teams within 36 hours; each event used once."""
    a, h = game["away"]["abbr"], game["home"]["abbr"]
    st = ts(game.get("start"))
    best, bd = None, None
    for e in evs:
        if e["eid"] in used or {e["away"]["abbr"], e["home"]["abbr"]} != {a, h}:
            continue
        diff = abs((e["start"] - st).total_seconds()) if st else 0
        if diff > 36 * 3600:
            continue
        if bd is None or diff < bd:
            best, bd = e, diff
    if not best and st:  # fallback: same kickoff (within 3 hours) and at least one team in common
        for e in evs:
            if e["eid"] in used or not ({e["away"]["abbr"], e["home"]["abbr"]} & {a, h}):
                continue
            diff = abs((e["start"] - st).total_seconds())
            if diff <= 3 * 3600 and (bd is None or diff < bd):
                best, bd = e, diff
    if best:
        used.add(best["eid"])
        if lg and {best["away"]["abbr"], best["home"]["abbr"]} != {a, h}:
            ours = ({a, h} - {best["away"]["abbr"], best["home"]["abbr"]})
            theirs = ({best["away"]["abbr"], best["home"]["abbr"]} - {a, h})
            if len(ours) == 1 and len(theirs) == 1:
                raw = next(iter(theirs))
                rev = {v: k for k, v in ABBR.get(lg, {}).items()}
                raw = rev.get(raw, raw)
                LEARNED.setdefault(lg, {})[raw] = next(iter(ours))
                ABBR[lg][raw] = next(iter(ours))
    return best

# ---------------------------------------------------------------- football box score
PLAY_PATTERNS = [
    (r"^(.+?) (\d+) Yd pass from (.+?)(?: \(.*\))?\s*$", lambda m: "%s %s-yd TD pass from %s" % (short(m[1]), m[2], short(m[3]))),
    (r"^(.+?) (\d+) Yd Run(?: \(.*\))?\s*$", lambda m: "%s %s-yd TD run" % (short(m[1]), m[2])),
    (r"^(.+?) (\d+) Yd Field Goal\s*$", lambda m: "%s %s-yd FG" % (short(m[1]), m[2])),
    (r"^(.+?) (\d+) Yd Interception Return(?: \(.*\))?\s*$", lambda m: "%s %s-yd INT return TD" % (short(m[1]), m[2])),
    (r"^(.+?) (\d+) Yd Fumble (?:Return|Recovery)(?: \(.*\))?\s*$", lambda m: "%s %s-yd fumble return TD" % (short(m[1]), m[2])),
    (r"^(.+?) (\d+) Yd (Punt|Kickoff) Return(?: \(.*\))?\s*$", lambda m: "%s %s-yd %s return TD" % (short(m[1]), m[2], m[3].lower())),
]

def play_text(t):
    t = t.strip()
    for pat, f in PLAY_PATTERNS:
        m = re.match(pat, t)
        if m:
            return f(m)
    return re.sub(r"\s*\(.*\)\s*$", "", t)

def football_box(lg, eid, game, fav_abbrs):
    s = fetch("https://site.api.espn.com/apis/site/v2/sports/%s/summary?event=%s" % (PATHS[lg], eid))
    comp = s["header"]["competitions"][0]
    by = {}
    for t in comp["competitors"]:
        ab = ABBR[lg].get(t["team"]["abbreviation"], t["team"]["abbreviation"])
        by[ab] = [num(l.get("displayValue")) for l in t.get("linescores", [])]
    a, h = game["away"]["abbr"], game["home"]["abbr"]
    n = max(len(by.get(a, [])), len(by.get(h, [])))
    periods = [str(i + 1) for i in range(min(n, 4))] + (["OT"] if n > 4 else [])
    def fold(x):
        x = list(x) + [0] * (n - len(x))
        return x[:4] + ([sum(x[4:])] if n > 4 else [])
    lines = {"periods": periods, "away": fold(by.get(a, [])), "home": fold(by.get(h, []))}
    # team stats
    ts_ = {}
    for t in s["boxscore"].get("teams", []):
        ab = ABBR[lg].get(t["team"]["abbreviation"], t["team"]["abbreviation"])
        ts_[ab] = {x["name"]: x["displayValue"] for x in t.get("statistics", [])}
    stats = []
    for label, key, conv in [("Total yards", "totalYards", num), ("Passing yards", "netPassingYards", num),
                             ("Rushing yards", "rushingYards", num), ("Turnovers", "turnovers", num),
                             ("First downs", "firstDowns", num), ("Possession", "possessionTime", str)]:
        if key in ts_.get(a, {}) and key in ts_.get(h, {}):
            stats.append({"label": label, "away": conv(ts_[a][key]), "home": conv(ts_[h][key])})
    # scoring plays (TD and FG), snap clock from the drive play text when present
    snap = {}
    for d in (s.get("drives") or {}).get("previous", []):
        for p in d.get("plays", []):
            m = re.match(r"^\((\d+):(\d\d)\)", p.get("text", ""))
            if m:
                snap[p["id"]] = "%d:%s" % (int(m[1]), m[2])
    plays = []
    for p in s.get("scoringPlays", []):
        typ = (p.get("scoringType") or {}).get("abbreviation")
        if typ not in ("TD", "FG"):
            continue
        per = p["period"]["number"]
        q = "Q%d" % per if per <= 4 else "OT"
        clock = snap.get(p["id"], (p.get("clock") or {}).get("displayValue", ""))
        team = ABBR[lg].get(p["team"]["abbreviation"], p["team"]["abbreviation"])
        plays.append("%s %s · %s: %s" % (q, clock, team, play_text(p["text"])))
    # players (favorites only)
    players = []
    for blk in s["boxscore"].get("players", []):
        ab = ABBR[lg].get(blk["team"]["abbreviation"], blk["team"]["abbreviation"])
        if ab not in fav_abbrs:
            continue
        cat = {x["name"]: x for x in blk.get("statistics", [])}
        groups = []
        def rows(name, want):
            c = cat.get(name)
            if not c:
                return []
            keys = c.get("keys", [])
            out = []
            for at in c.get("athletes", []):
                if (at["athlete"].get("displayName") or "").strip() in ("", "Team") or at["athlete"].get("displayName", "").startswith(" "):
                    continue
                nm = at["athlete"].get("shortName") or short(at["athlete"].get("displayName"))
                vals = dict(zip(keys, at.get("stats", [])))
                out.append([nm] + [vals.get(k) for k in want])
            return out
        def ints(r, idx):
            for i in idx:
                r[i] = num(r[i])
            return r
        p = [ints(r, [2, 3, 4]) for r in rows("passing", ["completions/passingAttempts", "passingYards", "passingTouchdowns", "interceptions"])]
        if p: groups.append({"title": "Passing", "cols": ["C/ATT", "YDS", "TD", "INT"], "rows": p})
        r_ = [ints(r, [1, 2, 4]) for r in rows("rushing", ["rushingAttempts", "rushingYards", "yardsPerRushAttempt", "rushingTouchdowns"])]
        if r_: groups.append({"title": "Rushing", "cols": ["CAR", "YDS", "AVG", "TD"], "rows": r_})
        rc = [ints(r, [1, 2, 3, 4]) for r in rows("receiving", ["receptions", "receivingYards", "receivingTouchdowns", "longReception"])]
        if rc: groups.append({"title": "Receiving", "cols": ["REC", "YDS", "TD", "LONG"], "rows": rc})
        df = [ints(r, [1, 2, 3, 4]) for r in rows("defensive", ["totalTackles", "soloTackles", "sacks", "tacklesForLoss"])]
        df = sorted(df, key=lambda r: -(r[1] or 0))[:5]
        if df: groups.append({"title": "Defense", "cols": ["TOT", "SOLO", "SACK", "TFL"], "rows": df})
        it = [ints(r, [1, 2, 3]) for r in rows("interceptions", ["interceptions", "interceptionYards", "interceptionTouchdowns"])]
        it = [r for r in it if (r[1] or 0) > 0]
        if it: groups.append({"title": "Interceptions", "cols": ["INT", "YDS", "TD"], "rows": it})
        kk = [ints(r, [2, 4]) for r in rows("kicking", ["fieldGoalsMade/fieldGoalAttempts", "longFieldGoalMade", "extraPointsMade/extraPointAttempts", "totalKickingPoints"])]
        if kk: groups.append({"title": "Kicking", "cols": ["FG", "LONG", "XP", "PTS"], "rows": kk})
        for g in groups:  # AVG stays a one-decimal string
            if g["title"] == "Rushing":
                for r in g["rows"]:
                    r[3] = "%.1f" % float(r[3]) if r[3] not in (None, "") else r[3]
        players.append({"team": ab, "groups": groups})
    return {"lines": lines, "stats": stats, "plays": plays[:12], "players": players}

# ---------------------------------------------------------------- baseball box score
def mlb_box(game, fav_abbrs):
    d = ts(game["start"]).astimezone(ET).strftime("%Y-%m-%d")
    tid = next((MLB_IDS[a] for a in fav_abbrs if a in MLB_IDS), None)
    sch = fetch("https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=%s&teamId=%d&hydrate=decisions,linescore" % (d, tid))
    gms = [g for dd in sch.get("dates", []) for g in dd.get("games", [])]
    if not gms:
        raise RuntimeError("no MLB game found for %s on %s" % (fav_abbrs, d))
    g0 = min(gms, key=lambda g: abs((ts(g["gameDate"]) - ts(game["start"])).total_seconds()))
    pk = g0["gamePk"]
    box = fetch("https://statsapi.mlb.com/api/v1/game/%d/boxscore" % pk)
    ls = g0.get("linescore") or fetch("https://statsapi.mlb.com/api/v1/game/%d/linescore" % pk)
    inn = ls.get("innings", [])
    lines = {"periods": [str(i["num"]) for i in inn],
             "away": [i.get("away", {}).get("runs", 0) for i in inn],
             "home": [i.get("home", {}).get("runs", "x") for i in inn]}
    sides = {}
    for side in ("away", "home"):
        t = box["teams"][side]
        sides[side] = t
    def abbr_of(side):
        return game[side]["abbr"]
    stats = []
    tl = ls.get("teams", {})
    stats.append({"label": "Hits", "away": tl.get("away", {}).get("hits"), "home": tl.get("home", {}).get("hits")})
    stats.append({"label": "Errors", "away": tl.get("away", {}).get("errors"), "home": tl.get("home", {}).get("errors")})
    def avg(side):
        b = sides[side]["teamStats"]["batting"]
        return ("%.3f" % (b["hits"] / b["atBats"])).lstrip("0") if b.get("atBats") else None
    stats.append({"label": "Batting avg", "away": avg("away"), "home": avg("home")})
    def risp(side):
        for blk in sides[side].get("info", []):
            for f in blk.get("fieldList", []):
                if f.get("label") == "Team RISP":
                    m = re.match(r"(\d+)-for-(\d+)", f.get("value", ""))
                    if m:
                        return "%s for %s" % (m[1], m[2])
        return None
    ra, rh = risp("away"), risp("home")
    if ra and rh:
        stats.append({"label": "With runners in scoring position", "away": ra, "home": rh})
    dec = g0.get("decisions") or {}
    if dec:
        last = lambda p: p["fullName"].split()[-1] if p else None
        sa, sh = game["away"].get("score") or 0, game["home"].get("score") or 0
        win, lose = ("away", "home") if sa > sh else ("home", "away")
        w = "W " + last(dec["winner"]) if dec.get("winner") else ""
        if dec.get("save"):
            w += " · S " + last(dec["save"])
        lo = "L " + last(dec["loser"]) if dec.get("loser") else ""
        stats.append({"label": "Pitching decision", win: w, lose: lo})
    players, problems = [], []
    for side in ("away", "home"):
        ab = abbr_of(side)
        if ab not in fav_abbrs:
            continue
        t = sides[side]
        pl = t["players"]
        bat = []
        for pid, p in pl.items():
            if "battingOrder" in p and p.get("stats", {}).get("batting"):
                bat.append((int(p["battingOrder"]), p))
        bat.sort(key=lambda x: x[0])
        brows = []
        for _, p in bat:
            b = p["stats"]["batting"]
            brows.append([short(p["person"]["fullName"]), b.get("atBats", 0), b.get("runs", 0), b.get("hits", 0),
                          b.get("rbi", 0), b.get("baseOnBalls", 0), b.get("strikeOuts", 0)])
        prows = []
        for pid in t.get("pitchers", []):
            p = pl["ID%d" % pid]
            s_ = p["stats"]["pitching"]
            prows.append([short(p["person"]["fullName"]), s_.get("inningsPitched"), s_.get("hits", 0),
                          s_.get("earnedRuns", 0), s_.get("baseOnBalls", 0), s_.get("strikeOuts", 0)])
        tb = t["teamStats"]["batting"]
        for i, k in [(1, "atBats"), (2, "runs"), (3, "hits"), (5, "baseOnBalls"), (6, "strikeOuts")]:
            if sum(r[i] for r in brows) != tb.get(k):
                problems.append("%s batting %s rows=%d team=%s" % (ab, k, sum(r[i] for r in brows), tb.get(k)))
        tp = t["teamStats"]["pitching"]
        for i, k in [(2, "hits"), (3, "earnedRuns"), (4, "baseOnBalls"), (5, "strikeOuts")]:
            if sum(r[i] for r in prows) != tp.get(k):
                problems.append("%s pitching %s rows=%d team=%s" % (ab, k, sum(r[i] for r in prows), tp.get(k)))
        groups = []
        if brows: groups.append({"title": "Batting", "cols": ["AB", "R", "H", "RBI", "BB", "K"], "rows": brows})
        if prows: groups.append({"title": "Pitching", "cols": ["IP", "H", "ER", "BB", "K"], "rows": prows})
        players.append({"team": ab, "groups": groups})
    return {"lines": lines, "stats": stats, "players": players}, problems

# ---------------------------------------------------------------- name matching
import unicodedata

SUFFIX = {"jr", "jr.", "sr", "ii", "iii", "iv"}

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", "", s).strip()

def last_name(s):
    p = [x for x in norm(s).split() if x not in SUFFIX]
    return p[-1] if p else ""

def same_person(a, b):
    na, nb = norm(a), norm(b)
    if na == nb:
        return True
    la, lb = last_name(a), last_name(b)
    if la and la == lb and na[:1] == nb[:1]:
        return True
    ta, tb = set(na.split()) - SUFFIX, set(nb.split()) - SUFFIX
    if la and la == lb and (ta <= tb or tb <= ta):
        return True
    import difflib
    if la and lb and na[:1] == nb[:1] and difflib.SequenceMatcher(None, la, lb).ratio() >= 0.8:
        return True
    return False

# ---------------------------------------------------------------- UFC
UFC_CARD = {"main": "main", "prelims1": "prelims", "prelims2": "early"}
UFC_METHOD = {"kotko": "KO/TKO", "ko-tko": "KO/TKO", "tko": "KO/TKO", "ko": "KO/TKO", "submission": "Submission",
              "decision---unanimous": "Decision (UD)", "decision---split": "Decision (SD)",
              "decision---majority": "Decision (MD)", "dq": "DQ", "disqualification": "DQ",
              "no-contest": "No contest", "nc": "No contest", "draw": "Draw",
              "draw---majority": "Draw (majority)", "draw---split": "Draw (split)", "draw---unanimous": "Draw (unanimous)"}

def ufc_event_id(ev):
    """Find the ESPN event id for a stored UFC event by date and name."""
    d = ts(ev["start"]).astimezone(ET).strftime("%Y%m%d")
    sbd = fetch("https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard?dates=%s" % d)
    best = None
    for e in sbd.get("events", []):
        if norm(e["name"]) == norm(ev["name"]) or last_name(e["name"]) == last_name(ev["name"]):
            return e["id"]
        best = best or e["id"]
    return best

def ufc_card(eid):
    return fetch("https://site.api.espn.com/apis/common/v3/sports/mma/ufc/fightcenter/%s" % eid)

def ufc_bouts(fc, stored_bouts):
    out, problems = [], []
    order = sorted(fc.get("cards", {}).items(), key=lambda kv: ["main", "prelims1", "prelims2"].index(kv[0]) if kv[0] in UFC_CARD else 9)
    for key, card in order:
        for c in sorted(card.get("competitions", []), key=lambda c: c.get("matchNumber", 99)):
            st = c.get("status", {})
            state = st.get("type", {}).get("state")
            name = st.get("type", {}).get("name", "")
            if "CANCEL" in name or "POSTPONE" in name:
                continue
            comp = c.get("competitors", [])
            if len(comp) != 2:
                continue
            comp = sorted(comp, key=lambda x: x.get("order", 9))
            names = [x["athlete"]["displayName"] for x in comp]
            old = None
            for b in stored_bouts:
                if (same_person(b["a"], names[0]) and same_person(b["b"], names[1])) or (same_person(b["a"], names[1]) and same_person(b["b"], names[0])):
                    old = b
                    break
            if old:
                flip = same_person(old["a"], names[1])
                bout = {k: v for k, v in old.items() if k not in ("status", "winner", "method")}
                a_c, b_c = (comp[1], comp[0]) if flip else (comp[0], comp[1])
            else:
                wt = c.get("type", {}).get("text") or ""
                bout = {"a": names[0], "b": names[1], "weight": wt, "start": iso(ts(c["date"])).replace("Z", "+00:00")}
                a_c, b_c = comp[0], comp[1]
            if key in UFC_CARD:
                bout["card"] = UFC_CARD[key]
            if "title" in (c.get("note") or "").lower():
                bout["title"] = True
            if state == "in":
                bout["status"] = "live"
            elif state == "post":
                res = st.get("result") or {}
                m = UFC_METHOD.get(res.get("name"), res.get("displayName") or "Result")
                bout["method"] = "%s · R%s %s" % (m, st.get("period"), st.get("displayClock"))
                if old and old.get("method") and old["method"] != bout["method"]:
                    o_base, o_rest = old["method"].split(" · ", 1) if " · " in old["method"] else (old["method"], "")
                    if o_base.startswith(m) and o_rest == bout["method"].split(" · ", 1)[-1]:
                        bout["method"] = old["method"]  # keep the more specific stored method
                if a_c.get("winner"):
                    bout["winner"] = "a"
                elif b_c.get("winner"):
                    bout["winner"] = "b"
                elif not m.startswith(("Draw", "No contest")):
                    problems.append("finished bout %s vs %s has no winner flag" % (bout["a"], bout["b"]))
            out.append(bout)
    return out, problems

def ufc_tick(db, out, now, rep, force=False):
    doc = load(db, "ufc")
    if not doc:
        return
    changed = False
    for ev in doc.get("events", []):
        st = ts(ev.get("start"))
        need = ev.get("status") == "live" or (ev.get("status") == "upcoming" and st and st <= now + dt.timedelta(minutes=15))
        need = need or (ev.get("status") == "final" and any(not b.get("method") for b in ev.get("bouts", [])) and st and st >= now - dt.timedelta(days=3))
        if not (need or force):
            continue
        try:
            eid = ufc_event_id(ev)
            fc = ufc_card(eid)
        except Exception as e:
            rep["errors"].append("ufc %s: %s" % (ev["name"], e))
            continue
        bouts, probs = ufc_bouts(fc, ev.get("bouts", []))
        rep["warnings"] += ["ufc " + p for p in probs]
        if not bouts:
            rep["warnings"].append("ufc %s: no bouts from source; kept stored card" % ev["name"])
            continue
        was = ev.get("status")
        states = [b for b in bouts]
        done = sum(1 for b in bouts if b.get("method"))
        live = [b for b in bouts if b.get("status") == "live"]
        if done == len(bouts):
            ev["status"] = "final"
        elif live or done or (st and now >= st):
            ev["status"] = "live"
        else:
            ev["status"] = "upcoming"
        ev["bouts"] = bouts
        if not ev.get("venue") and fc.get("venue", {}).get("displayNameLocation"):
            ev["venue"] = fc["venue"]["displayNameLocation"]
        ev["seen"] = iso(now)
        line = "ufc %s: %s, %d of %d bouts done%s (was %s)" % (ev["name"], ev["status"], done, len(bouts),
               "; live: %s vs %s" % (live[0]["a"], live[0]["b"]) if live else "", was)
        rep["changed"].append(line)
        if ev["status"] == "live":
            rep["live"].append(line)
        if ev["status"] == "final" and was != "final":
            rep["newFinals"].append({"lg": "ufc", "id": ev["name"]})
        changed = True
    if changed:
        doc["updatedAt"] = iso(now)
        dump(doc, os.path.join(out, "ufc.json"))
        rep.setdefault("changed_docs", []).append("ufc")

# ---------------------------------------------------------------- NASCAR
NAS_FLAG = {1: "Green", 2: "Caution", 3: "Red flag", 4: "Checkered", 5: "White", 8: "Warm up", 9: "Not active"}

def clean_driver(n):
    return re.sub(r"\s*(\(i\)|\(P\)|\(R\)|\(C\)|\*|#)\s*", " ", n or "").strip()

def canon(name, known):
    """Spell a driver exactly as the app already does when we know them."""
    for k in known:
        if same_person(k, name):
            return k
    return name

def nascar_race_for(nx, season):
    """Find the NASCAR Cup race id for the stored 'next' race."""
    rl = fetch("https://cf.nascar.com/cacher/%d/1/race_list_basic.json" % season)
    st = ts(nx.get("start")) if nx.get("start") else None
    best, bd = None, None
    for r in rl:
        rs = dt.datetime.fromisoformat(r["race_date"]).replace(tzinfo=ET)
        if st:
            diff = abs((rs - st).total_seconds())
        else:
            diff = 0 if norm(r["track_name"]) in norm(nx.get("track", "")) else 9e9
        if bd is None or diff < bd:
            best, bd = r, diff
    return best if bd is not None and bd < 36 * 3600 else None

def caution_reason(reason, comment):
    t = ("%s %s" % (reason or "", comment or "")).lower()
    c = (comment or "").lower()
    if "conclusion" in c and not any(k in c for k in ("incident", "spin", "accident", "debris", "fluid", "stopped")):
        return "Stage end"
    if "competition" in t:
        return "Competition caution"
    for k, v in [("spin", "Spin"), ("incident", "Accident"), ("accident", "Accident"), ("crash", "Accident"),
                 ("debris", "Debris"), ("fluid", "Fluid"), ("oil", "Fluid"), ("stopped", "Stopped car"),
                 ("stall", "Stopped car"), ("weather", "Weather"), ("rain", "Weather"), ("stage", "Stage end")]:
        if k in t:
            return v
    return (reason or "Caution").strip() or "Caution"

def nascar_cautions(items, names_by_car):
    """items: [{lap, reason, comment}] from the live flag feed or the official caution segments."""
    out = []
    for it in items:
        r = caution_reason(it.get("reason"), it.get("comment"))
        drivers = []
        if r not in ("Competition caution", "Stage end", "Debris"):
            text = (it.get("comment") or "").split(" / ")[0]
            for car in re.findall(r"#?(\d+)", text.split(" incident")[0].split(" spin")[0]):
                nm = names_by_car.get(car)
                if nm and nm not in drivers:
                    drivers.append(nm)
        out.append({"lap": it.get("lap"), "reason": r, "drivers": drivers})
    return out

def live_caution_items():
    fl = fetch("https://cf.nascar.com/live/feeds/live-flag-data.json")
    items, prev = [], None
    for f in fl:
        if f.get("flag_state") == 2 and prev != 2:
            items.append({"lap": f.get("lap_number"), "reason": None, "comment": f.get("comment")})
        prev = f.get("flag_state")
    return items

def nascar_tick(db, out, now, rep, favs):
    doc = load(db, "nascar")
    if not doc or not doc.get("next", {}).get("start"):
        return
    nx = doc["next"]
    st = ts(nx["start"])
    if now < st - dt.timedelta(minutes=10) or (now > st + dt.timedelta(hours=8) and nx.get("status") != "live"):
        return
    try:
        lf = fetch("https://cf.nascar.com/live/feeds/live-feed.json")
        race = nascar_race_for(nx, st.year)
    except Exception as e:
        rep["errors"].append("nascar feed: %s" % e)
        return
    was = nx.get("status")
    if not race or lf.get("series_id") != 1 or lf.get("race_id") != race["race_id"] or lf.get("run_type") != 3:
        if now < st:
            return
        nx["status"] = "live"
        nx.setdefault("live", {})
        nx["live"]["asOf"] = iso(now)
        nx["live"]["note"] = "Waiting for the race feed (the live feed is showing %s)." % (lf.get("run_name") or "another session")
        rep["warnings"].append("nascar: live feed is on '%s', not the Cup race yet" % lf.get("run_name"))
        rep["live"].append("nascar %s: started, waiting for race feed" % nx["name"])
        doc["updatedAt"] = iso(now)
        dump(doc, os.path.join(out, "nascar.json"))
        rep.setdefault("changed_docs", []).append("nascar")
        return
    vs = sorted(lf.get("vehicles", []), key=lambda v: v.get("running_position", 99))
    leader_laps = vs[0]["laps_completed"] if vs else 0
    order, by_car = [], {}
    known = [r["driver"] for r in doc.get("standings", [])] + [g["driver"] for g in nx.get("grid", []) or []]
    for v in vs:
        nm = canon(clean_driver(v["driver"]["full_name"]), known)
        by_car[str(v["vehicle_number"])] = nm
        led = sum(r["end_lap"] - r["start_lap"] + 1 for r in v.get("laps_led", []))
        row = {"pos": v["running_position"], "driver": nm, "team": "No. %s" % v["vehicle_number"]}
        down = leader_laps - v.get("laps_completed", 0)
        if v["running_position"] > 1:
            row["gap"] = "+%d lap%s" % (down, "s" if down > 1 else "") if down > 0 else "+%.3f" % abs(v.get("delta", 0))
        if led:
            row["led"] = led
        row["pits"] = len([p for p in v.get("pit_stops", []) if p.get("pit_in_leader_lap", 0) > 0])
        if v.get("status") not in (1, None):
            row["status"] = "Out"
        order.append(row)
    finished = lf.get("flag_state") == 4 or (lf.get("laps_to_go", 1) == 0 and leader_laps >= lf.get("laps_in_race", 9e9))
    try:
        cautions = nascar_cautions(live_caution_items(), by_car)
    except Exception as e:
        cautions = None
        rep["warnings"].append("nascar cautions: %s" % e)
    n_caut = lf.get("number_of_caution_segments", 0)
    stats = [{"label": "Cautions", "value": str(n_caut)}, {"label": "Lead changes", "value": str(lf.get("number_of_lead_changes", 0))}]
    live = {"lap": lf.get("lap_number"), "laps": lf.get("laps_in_race"), "flag": NAS_FLAG.get(lf.get("flag_state"), ""),
            "asOf": iso(now), "source": "NASCAR live feed", "order": order, "stats": stats, "raceId": race["race_id"]}
    if cautions is not None:
        while len(cautions) < n_caut:
            cautions.append({"lap": None, "reason": "Caution", "drivers": []})
        live["cautions"] = cautions[:max(n_caut, len(cautions))]
        if len(cautions) != n_caut:
            rep["warnings"].append("nascar: lap notes show %d cautions, live feed counts %d" % (len(cautions), n_caut))
    if nx.get("live", {}).get("desk"):
        live["desk"] = nx["live"]["desk"]  # the model rewrites it after the script runs
    nx["status"] = "live"
    nx["live"] = live
    favd = [d for d in favs.get("nascar", set())]
    fav_pos = ["%s P%s" % (r["driver"], r["pos"]) for r in order if any(same_person(r["driver"], f) for f in favd)]
    rep["deskFacts"] = rep.get("deskFacts", {})
    rep["deskFacts"]["nascar"] = {"lap": live["lap"], "laps": live["laps"], "flag": live["flag"],
        "leader": order[0]["driver"] if order else None, "gap2": order[1].get("gap") if len(order) > 1 else None,
        "leaderLed": order[0].get("led") if order else None, "cautions": n_caut, "leadChanges": lf.get("number_of_lead_changes"),
        "lastCaution": cautions[-1] if cautions else None, "favorites": fav_pos,
        "out": [r["driver"] for r in order if r.get("status") == "Out"], "finished": finished,
        "previousDesk": (nx.get("live") or {}).get("desk")}
    line = "nascar %s: lap %s/%s %s, leader %s (was %s)" % (nx["name"], live["lap"], live["laps"], live["flag"], order[0]["driver"] if order else "?", was or "pre")
    rep["changed"].append(line)
    if finished:
        rep["newFinals"].append({"lg": "nascar", "id": race["race_id"], "name": nx["name"]})
    else:
        rep["live"].append(line)
    doc["updatedAt"] = iso(now)
    dump(doc, os.path.join(out, "nascar.json"))
    rep.setdefault("changed_docs", []).append("nascar")

# ---------------------------------------------------------------- F1
def f1_tick(db, out, now, rep):
    doc = load(db, "f1")
    if not doc or not doc.get("next", {}).get("start"):
        return
    nx = doc["next"]
    st = ts(nx["start"])
    if now < st - dt.timedelta(minutes=10) or (now > st + dt.timedelta(hours=5) and nx.get("status") != "live"):
        return
    try:
        sbd = fetch("https://site.api.espn.com/apis/site/v2/sports/racing/f1/scoreboard?dates=%s" % st.strftime("%Y%m%d"))
    except Exception as e:
        rep["errors"].append("f1 scoreboard: %s" % e)
        return
    race = None
    for e in sbd.get("events", []):
        for c in e.get("competitions", []):
            if c.get("type", {}).get("abbreviation") == "Race" and abs((ts(c["date"]) - st).total_seconds()) < 6 * 3600:
                race = c
    if not race:
        rep["warnings"].append("f1: race not found on ESPN scoreboard")
        return
    state = race["status"]["type"]["state"]
    if state == "pre":
        return
    team_of = {r["driver"]: r.get("team") for r in doc.get("standings", [])}
    for g in nx.get("grid", []) or []:
        team_of[g["driver"]] = g.get("team")
    order = []
    for c in sorted(race["competitors"], key=lambda c: c.get("order", 99)):
        nm = c["athlete"]["displayName"]
        known = next((k for k in team_of if same_person(k, nm)), nm)
        order.append({"pos": c.get("order"), "driver": known, "team": team_of.get(known, "")})
    was = nx.get("status")
    finished = state == "post"
    live = {"lap": race["status"].get("period"), "asOf": iso(now), "source": "ESPN", "order": order}
    if nx.get("laps"):
        live["laps"] = nx["laps"]
    nx["status"] = "live"
    nx["live"] = live
    line = "f1 %s: lap %s, leader %s (was %s)" % (nx["name"], live["lap"], order[0]["driver"] if order else "?", was or "pre")
    rep["changed"].append(line)
    if finished:
        rep["newFinals"].append({"lg": "f1", "name": nx["name"]})
    else:
        rep["live"].append(line)
    doc["updatedAt"] = iso(now)
    dump(doc, os.path.join(out, "f1.json"))
    rep.setdefault("changed_docs", []).append("f1")

# ---------------------------------------------------------------- Supercross / Motocross (no live data source)
def moto_tick(db, out, now, rep):
    doc = load(db, "moto")
    if not doc or not doc.get("next", {}).get("start"):
        return
    nx = doc["next"]
    st = ts(nx["start"])
    if st <= now <= st + dt.timedelta(hours=4) and nx.get("status") != "live":
        nx["status"] = "live"
        nx["live"] = {"asOf": iso(now), "note": "Racing now. Results arrive after the main events."}
        rep["live"].append("moto %s: racing (no live timing source)" % nx["name"])
        doc["updatedAt"] = iso(now)
        dump(doc, os.path.join(out, "moto.json"))
        rep.setdefault("changed_docs", []).append("moto")
    elif now > st + dt.timedelta(hours=4):
        rep["newFinals"].append({"lg": "moto", "name": nx["name"]})

# ---------------------------------------------------------------- race finals (results, points, standings, favorite season docs)
def date_text(d):
    return d.strftime("%a, %b ") + str(d.day)

def nascar_tv(s):
    return {"USA": "USA Network", "NBC": "NBC", "CNBC": "CNBC", "FOX": "FOX", "FS1": "FS1", "PRIME": "Prime Video",
            "TNT": "TNT Sports", "AMAZON": "Prime Video"}.get((s or "").upper(), s)

def nascar_start(r):
    return dt.datetime.fromisoformat(r["race_date"]).replace(tzinfo=ET)

def nascar_final(db, out, now, rep, favs):
    doc = load(db, "nascar")
    nx = doc.get("next", {})
    st = ts(nx["start"])
    season = st.year
    rl = [r for r in fetch("https://cf.nascar.com/cacher/%d/1/race_list_basic.json" % season) if r.get("race_type_id") == 1]
    rl.sort(key=nascar_start)
    race = nascar_race_for(nx, season)
    if not race:
        rep["errors"].append("nascar final: race not found")
        return
    w = fetch("https://cf.nascar.com/cacher/%d/1/%d/weekend-feed.json" % (season, race["race_id"]))["weekend_race"][0]
    res = sorted(w.get("results", []), key=lambda r: r["finishing_position"])
    if not res or not any(r.get("points_earned") for r in res):
        rep["warnings"].append("nascar final: official results not posted yet; keep the race live and retry")
        return
    pts_feed = fetch("https://cf.nascar.com/cacher/%d/1/racinginsights-points-feed.json" % season)
    known = [r["driver"] for r in doc.get("standings", [])] + [r["driver"] for r in (nx.get("live") or {}).get("order", [])]
    known += [clean_driver(p["driver_name"]) for p in pts_feed]
    name = lambda n: canon(clean_driver(n), known)
    cls = []
    for r in res:
        row = {"pos": r["finishing_position"], "driver": name(r["driver_fullname"]), "team": "No. %s" % r["car_number"],
               "start": r["starting_position"], "laps": r["laps_completed"],
               "status": "Finished" if r["finishing_status"] == "Running" else r["finishing_status"],
               "pts": r.get("points_earned", 0)}
        if r["finishing_position"] == 1:
            row["time"] = ""
        elif res[0]["laps_completed"] - r["laps_completed"] > 0:
            down = res[0]["laps_completed"] - r["laps_completed"]
            row["time"] = "+%d lap%s" % (down, "s" if down > 1 else "")
        else:
            row["time"] = "+%s" % (("%.3f" % (r.get("diff_time", 0) / 1000)).rstrip("0").rstrip("."))
        if r.get("laps_led"):
            row["led"] = r["laps_led"]
        cls.append(row)
    by_car = {r["car_number"]: name(r["driver_fullname"]) for r in res}
    cautions = nascar_cautions([{"lap": c["start_lap"], "reason": c.get("reason"), "comment": c.get("comment")}
                                for c in w.get("caution_segments", [])], by_car)
    most = max(cls, key=lambda r: r.get("led", 0))
    stats = [{"label": "Margin of victory", "value": "%s s over %s" % (("0" + w["margin_of_victory"]) if str(w.get("margin_of_victory", "")).startswith(".") else w.get("margin_of_victory"), cls[1]["driver"])},
             {"label": "Cautions", "value": str(w.get("number_of_cautions"))},
             {"label": "Lead changes", "value": str(w.get("number_of_lead_changes"))},
             {"label": "Most laps led", "value": "%s %s" % (most["driver"], most.get("led", 0))}]
    for sr in w.get("stage_results", []):
        win = [x for x in sr.get("results", []) if x.get("finishing_position") == 1]
        if win and sr.get("stage_number") in (1, 2, 3):
            stats.append({"label": "Stage %d winner" % sr["stage_number"], "value": name(win[0]["driver_fullname"])})
    last = {"name": nx.get("name") or race["race_name"], "track": nx.get("track") or race["track_name"], "series": "NASCAR Cup Series",
            "start": nx.get("start"), "date": nx.get("date") or date_text(nascar_start(race)), "winner": cls[0]["driver"],
            "podium": [c["driver"] for c in cls[:3]], "classification": cls, "stats": stats, "cautions": cautions,
            "resultsNote": "Official results and points from NASCAR (nascar.com)."}
    doc["last"] = last
    doc["standings"] = [{"pos": p["position"], "driver": name(p["driver_name"]), "points": p["points"], "wins": p["wins"]}
                        for p in sorted(pts_feed, key=lambda p: p["position"])[:25]]
    after = [r for r in rl if nascar_start(r) > nascar_start(race)]
    if after:
        n = after[0]
        doc["next"] = {"name": n["race_name"], "track": n["track_name"], "start": nascar_start(n).isoformat(),
                       "date": date_text(nascar_start(n)), "tv": nascar_tv(n.get("television_broadcaster"))}
    else:
        doc["next"] = {}
    doc["note"] = "Standings after %s, from NASCAR's official points." % last["name"]
    doc["updatedAt"] = iso(now)
    dump(doc, os.path.join(out, "nascar.json"))
    rep.setdefault("changed_docs", []).append("nascar")
    rep["changed"].append("nascar final: %s won %s; standings and next race (%s) updated" % (last["winner"], last["name"], doc["next"].get("name")))
    rep["deskNeeded"] = {"nascar": {"winner": last["winner"], "margin": stats[0]["value"], "mostLed": stats[3]["value"],
                                    "cautions": len(cautions), "favorites": ["%s P%d" % (c["driver"], c["pos"]) for c in cls
                                    if any(same_person(c["driver"], f) for f in favs.get("nascar", set()))]}}
    # favorite driver season docs
    done = [r for r in rl if nascar_start(r) <= nascar_start(race)]
    feeds = {}
    for fav in favs.get("nascar", set()):
        slug = "fav_" + re.sub(r"[^A-Za-z0-9]+", "_", "nascar:" + fav)
        fd = load(db, slug)
        if not fd:
            rep["warnings"].append("%s missing; the final wrap must build it" % slug)
            continue
        rows = []
        for r in done:
            if r["race_id"] not in feeds:
                try:
                    feeds[r["race_id"]] = fetch("https://cf.nascar.com/cacher/%d/1/%d/weekend-feed.json" % (season, r["race_id"]))["weekend_race"][0]["results"]
                except Exception as e:
                    rep["warnings"].append("nascar %s results: %s" % (r["race_name"], e))
                    feeds[r["race_id"]] = []
            me = [x for x in feeds[r["race_id"]] if same_person(clean_driver(x["driver_fullname"]), fav)]
            if me:
                rows.append((r, me[0]))
        for r, me in rows:
            day = nascar_start(r).strftime("%Y-%m-%d")
            for rd in fd.get("races", []):
                if rd.get("day") == day:
                    status = me["finishing_status"]
                    rd["result"] = "P%d" % me["finishing_position"]
                    note = "Started %d" % me["starting_position"]
                    if me.get("laps_led"):
                        note += " · Led %d" % me["laps_led"]
                    if me["finishing_position"] == 1:
                        note += " · Win"
                    if status not in ("Running",):
                        note += " · %s" % ("Crash" if status in ("Accident", "Crash") else status)
                    rd["note"] = note
        pf = next((p for p in pts_feed if same_person(clean_driver(p["driver_name"]), fav)), None)
        n = len(rows)
        wins = [r["track_name"].replace(" Raceway", "").replace(" Speedway", "").replace(" Motor", "") for r, me in rows if me["finishing_position"] == 1]
        crashes = sum(1 for r, me in rows if me["finishing_status"] in ("Accident", "Crash"))
        stats_ = [{"label": "Races", "value": "%d of %d" % (n, len(rl))},
                  {"label": "Wins", "value": "%d%s" % (len(wins), " (%s)" % ", ".join(wins) if wins else "")},
                  {"label": "Top 5s", "value": str(sum(1 for r, me in rows if me["finishing_position"] <= 5))},
                  {"label": "Top 10s", "value": str(sum(1 for r, me in rows if me["finishing_position"] <= 10))},
                  {"label": "Poles", "value": str(pf["poles"]) if pf else "?"},
                  {"label": "Laps led", "value": str(sum(me.get("laps_led", 0) for r, me in rows))},
                  {"label": "Average start", "value": "%.1f" % (sum(me["starting_position"] for r, me in rows) / n) if n else "-"},
                  {"label": "Average finish", "value": "%.1f" % (sum(me["finishing_position"] for r, me in rows) / n) if n else "-"},
                  {"label": "Crashes out", "value": str(crashes)}]
        if pf:
            for lab, mine, theirs in [("Wins", len(wins), pf["wins"]), ("Top 5s", sum(1 for r, me in rows if me["finishing_position"] <= 5), pf["top_5"]),
                                      ("Top 10s", sum(1 for r, me in rows if me["finishing_position"] <= 10), pf["top_10"]),
                                      ("Laps led", sum(me.get("laps_led", 0) for r, me in rows), pf["laps_led"])]:
                if mine != theirs:
                    rep["warnings"].append("%s %s: race-by-race %s, NASCAR season feed %s" % (fav, lab, mine, theirs))
        fd["stats"] = stats_
        fd["statsNote"] = "Through %s. Official NASCAR results." % last["name"]
        fd["counted"] = sorted(set(fd.get("counted", [])) | {"%s %s" % (r["race_name"], nascar_start(r).strftime("%B %-d")) for r, me in rows})
        fd["updatedAt"] = iso(now)
        dump(fd, os.path.join(out, slug + ".json"))
        rep.setdefault("changed_docs", []).append(slug)

F1_TEAM = [("red bull", "Red Bull"), ("racing bulls", "Racing Bulls"), ("rb f1", "Racing Bulls"), ("visa", "Racing Bulls"),
           ("mercedes", "Mercedes"), ("ferrari", "Ferrari"), ("mclaren", "McLaren"), ("alpine", "Alpine"), ("haas", "Haas"),
           ("audi", "Audi"), ("sauber", "Audi"), ("williams", "Williams"), ("aston", "Aston Martin"), ("cadillac", "Cadillac")]

def f1_team(n):
    t = (n or "").lower()
    for k, v in F1_TEAM:
        if k in t:
            return v
    return n

def f1_name(d):
    return "%s %s" % (d["givenName"], d["familyName"])

def jol(path):
    return fetch("https://api.jolpi.ca/ergast/f1/%s" % path)["MRData"]

def f1_final(db, out, now, rep, favs):
    doc = load(db, "f1")
    nx = doc.get("next", {})
    st = ts(nx["start"])
    season = st.year
    races = jol("%d/races.json?limit=100" % season)["RaceTable"]["Races"]
    rstart = lambda r: ts("%sT%s" % (r["date"], r.get("time", "12:00:00Z")))
    rnd = min(races, key=lambda r: abs((rstart(r) - st).total_seconds()))
    rr = jol("%d/%s/results.json?limit=40" % (season, rnd["round"]))["RaceTable"]["Races"]
    if not rr or not rr[0].get("Results"):
        rep["warnings"].append("f1 final: official results not posted yet; keep the race live and retry")
        return
    R = rr[0]["Results"]
    known = [r["driver"] for r in doc.get("standings", [])]
    nm = lambda d: canon(f1_name(d), known)
    cls = []
    for r in R:
        pt = r.get("positionText", r["position"])
        row = {"pos": pt, "driver": nm(r["Driver"]), "team": f1_team(r["Constructor"]["name"]),
               "grid": int(r.get("grid", 0)), "laps": int(r.get("laps", 0)), "status": r.get("status"), "pts": num(r.get("points", 0))}
        if pt == "R":
            row["pos"] = "NC"
        stt = r.get("status") or ""
        down = int(R[0].get("laps", 0)) - row["laps"]
        if stt == "Finished" and r.get("Time"):
            row["time"] = r["Time"]["time"]
        elif stt == "Lapped" or "Lap" in stt:
            row["status"] = "Finished"
            row["time"] = "+%d lap%s" % (down, "s" if down > 1 else "")
        elif stt == "Finished":
            row["time"] = ""
        else:
            row["time"] = "DNF" if pt.isdigit() or pt in ("R", "N") else ("DSQ" if pt == "D" else stt)
        if (r.get("FastestLap") or {}).get("rank") == "1":
            row["fl"] = True
        cls.append(row)
    sgrid = {g["driver"]: g["pos"] for g in nx.get("grid", []) or [] if "provisional" not in (nx.get("gridNote") or "").lower()}
    for c in cls:  # prefer the official starting grid stored before the race (it includes penalties)
        if c["driver"] in sgrid:
            c["grid"] = sgrid[c["driver"]]
    fin = [c for c in cls if str(c["pos"]).isdigit()]
    stats = [{"label": "Winner's time", "value": cls[0].get("time", "")}]
    if len(fin) > 1 and str(fin[1].get("time", "")).startswith("+"):
        stats.append({"label": "Margin of victory", "value": "%s s (%s over %s)" % (fin[1]["time"].lstrip("+").rstrip("s"), cls[0]["driver"].split()[-1], fin[1]["driver"].split()[-1])})
    pole = next((c for c in cls if c["grid"] == 1), None)
    if pole:
        stats.append({"label": "Pole", "value": pole["driver"]})
    fl = [r for r in R if (r.get("FastestLap") or {}).get("rank") == "1"]
    if fl:
        stats.append({"label": "Fastest lap", "value": "%s %s, lap %s" % (nm(fl[0]["Driver"]), fl[0]["FastestLap"]["Time"]["time"], fl[0]["FastestLap"]["lap"])})
    gains = [(c["grid"] - int(c["pos"]), c["driver"]) for c in fin if c["grid"] > 0]
    if gains:
        g = max(x[0] for x in gains)
        if g > 0:
            stats.append({"label": "Biggest gain", "value": ", ".join("%s +%d" % (d, g) for x, d in gains if x == g)})
    ret = [c for c in cls if c["status"] not in ("Finished",) and "Lap" not in (c["status"] or "")]
    if ret:
        stats.append({"label": "Retirements", "value": ", ".join("%s (%s, %d laps)" % (c["driver"].split()[-1], c["status"], c["laps"]) for c in sorted(ret, key=lambda c: c["laps"]))})
    last = {"name": nx.get("name") or rnd["raceName"], "circuit": nx.get("circuit") or rnd["Circuit"]["circuitName"],
            "start": nx.get("start"), "date": nx.get("date") or date_text(rstart(rnd)), "winner": cls[0]["driver"],
            "podium": [c["driver"] for c in cls[:3]], "classification": cls, "stats": stats,
            "resultsNote": "Official classification and points (FIA results via the Jolpica F1 API)."}
    doc["last"] = last
    ds = jol("%d/driverstandings.json?limit=40" % season)["StandingsTable"]["StandingsLists"][0]
    cs = jol("%d/constructorstandings.json?limit=40" % season)["StandingsTable"]["StandingsLists"][0]
    if ds["round"] != rnd["round"] or cs["round"] != rnd["round"]:
        rep["warnings"].append("f1 standings are through round %s/%s, race was round %s; retry later" % (ds["round"], cs["round"], rnd["round"]))
    race_team = {r["Driver"]["driverId"]: f1_team(r["Constructor"]["name"]) for r in R}
    old_team = {r["driver"]: r.get("team") for r in doc.get("standings", [])}
    doc["standings"] = [{"pos": int(s["position"]), "driver": nm(s["Driver"]),
                         "team": race_team.get(s["Driver"]["driverId"]) or old_team.get(nm(s["Driver"])) or f1_team(s["Constructors"][-1]["name"]),
                         "points": num(s["points"])} for s in ds["DriverStandings"]]
    old_drivers = {c["team"]: c.get("drivers") for c in doc.get("constructors", [])}
    doc["constructors"] = [{"pos": int(s["position"]), "team": f1_team(s["Constructor"]["name"]), "points": num(s["points"]),
                            "drivers": old_drivers.get(f1_team(s["Constructor"]["name"]), "")} for s in cs["ConstructorStandings"]]
    doc["constructorsNote"] = "Includes the %s." % last["name"]
    doc["note"] = "Tables include the %s." % last["name"]
    nxt = [r for r in races if int(r["round"]) == int(rnd["round"]) + 1]
    if nxt:
        n = nxt[0]
        doc["next"] = {"name": n["raceName"], "circuit": n["Circuit"]["circuitName"], "start": iso(rstart(n)).replace("Z", "+00:00"),
                       "date": date_text(rstart(n).astimezone(ET)), "tv": nx.get("tv", "")}
    else:
        doc["next"] = {}
    doc["updatedAt"] = iso(now)
    dump(doc, os.path.join(out, "f1.json"))
    rep.setdefault("changed_docs", []).append("f1")
    rep["changed"].append("f1 final: %s won the %s; standings updated through round %s" % (last["winner"], last["name"], ds["round"]))
    # favorite season docs
    total = len(races)
    for kind, fav in [("f1", f) for f in favs.get("f1", set())] + [("f1team", f) for f in favs.get("f1team", set())]:
        slug = "fav_" + re.sub(r"[^A-Za-z0-9]+", "_", "%s:%s" % (kind, fav))
        fd = load(db, slug)
        if not fd:
            rep["warnings"].append("%s missing; the final wrap must build it" % slug)
            continue
        if kind == "f1":
            mine = [r for r in R if same_person(f1_name(r["Driver"]), fav)]
            did = mine[0]["Driver"]["driverId"] if mine else None
            if not did:
                continue
            res = jol("%d/drivers/%s/results.json?limit=100" % (season, did))["RaceTable"]["Races"]
            spr = jol("%d/drivers/%s/sprint.json?limit=100" % (season, did))["RaceTable"]["Races"]
            q = jol("%d/drivers/%s/qualifying.json?limit=100" % (season, did))["RaceTable"]["Races"]
            for rd in fd.get("races", []):
                if int(rd.get("round", 0)) == int(rnd["round"]):
                    m = mine[0]
                    pt = m.get("positionText")
                    rd["result"] = "P%s" % pt if pt.isdigit() else ("DSQ" if pt == "D" else "DNF")
                    bits = ["%s pts" % num(m["points"])]
                    if m.get("grid") == "1":
                        bits.append("Pole")
                    s_ = [s for s in spr if s["round"] == rnd["round"]]
                    if s_:
                        sp = s_[0]["SprintResults"][0]
                        bits.append("Sprint %s (%s pts)" % ("win" if sp["position"] == "1" else "P" + sp["position"], num(sp["points"])))
                    rd["note"] = " · ".join(bits)
            rname = {int(x["round"]): x["name"].replace(" Grand Prix", "") for x in fd.get("races", [])}
            short_ = lambda r: rname.get(int(r["round"]), r["raceName"].replace(" Grand Prix", ""))
            gp_pts = sum(num(r["Results"][0]["points"]) for r in res)
            sp_pts = sum(num(r["SprintResults"][0]["points"]) for r in spr)
            wins = [short_(r) for r in res if r["Results"][0]["position"] == "1"]
            poles = [short_(r) for r in q if r["QualifyingResults"][0]["position"] == "1"]
            sw = [short_(r) for r in spr if r["SprintResults"][0]["position"] == "1"]
            dnf = sum(1 for r in res if r["Results"][0]["status"] != "Finished" and "Lap" not in r["Results"][0]["status"])
            pod = sum(1 for r in res if r["Results"][0]["positionText"].isdigit() and int(r["Results"][0]["positionText"]) <= 3)
            total_pts = next((s["points"] for s in doc["standings"] if same_person(s["driver"], fav)), None)
            if total_pts is not None and gp_pts + sp_pts != total_pts:
                rep["warnings"].append("%s: GP %s + sprint %s != standings %s" % (fav, gp_pts, sp_pts, total_pts))
            fd["stats"] = [{"label": "Races", "value": "%d of %d" % (len(res), total)},
                           {"label": "Wins", "value": "%d%s" % (len(wins), " (%s)" % ", ".join(wins) if wins else "")},
                           {"label": "Podiums", "value": str(pod)},
                           {"label": "Poles", "value": "%d%s" % (len(poles), " (%s)" % ", ".join(poles) if poles else "")},
                           {"label": "Sprint wins", "value": "%d%s" % (len(sw), " (%s)" % ", ".join(sw) if sw else "")},
                           {"label": "DNFs", "value": str(dnf)},
                           {"label": "Grand Prix points", "value": str(gp_pts)},
                           {"label": "Sprint points", "value": str(sp_pts)}]
            fd["statsNote"] = "Through the %s. %s + %s = %s points." % (last["name"], gp_pts, sp_pts, gp_pts + sp_pts)
        else:
            tid = None
            for r in R:
                if f1_team(r["Constructor"]["name"]) == fav:
                    tid = r["Constructor"]["constructorId"]
            if not tid:
                continue
            res = jol("%d/constructors/%s/results.json?limit=200" % (season, tid))["RaceTable"]["Races"]
            spr = jol("%d/constructors/%s/sprint.json?limit=200" % (season, tid))["RaceTable"]["Races"]
            q = jol("%d/constructors/%s/qualifying.json?limit=200" % (season, tid))["RaceTable"]["Races"]
            mine = [r for r in R if r["Constructor"]["constructorId"] == tid]
            for rd in fd.get("races", []):
                if int(rd.get("round", 0)) == int(rnd["round"]):
                    p = sum(num(m["points"]) for m in mine)
                    s_ = [s for s in spr if s["round"] == rnd["round"]]
                    p += sum(num(x["points"]) for s in s_ for x in s["SprintResults"])
                    rd["result"] = "%s pts" % p
                    code = lambda d: d.get("code", d["familyName"][:3].upper())
                    note = " · ".join("%s %s" % (code(m["Driver"]), "P" + m["positionText"] if m["positionText"].isdigit() else "DNF") for m in mine)
                    sp_ = [x for s in s_ for x in s["SprintResults"] if num(x["points"]) > 0 and x["Constructor"]["constructorId"] == tid]
                    if sp_:
                        note += " · Sprint: " + ", ".join("%s P%s (%s pts)" % (code(x["Driver"]), x["position"], num(x["points"])) for x in sp_)
                    rd["note"] = note
            per = {}
            for r in res:
                for x in r["Results"]:
                    per[f1_name(x["Driver"])] = per.get(f1_name(x["Driver"]), 0) + num(x["points"])
            for r in spr:
                for x in r["SprintResults"]:
                    per[f1_name(x["Driver"])] = per.get(f1_name(x["Driver"]), 0) + num(x["points"])
            rname = {int(x["round"]): x["name"].replace(" Grand Prix", "") for x in fd.get("races", [])}
            wins = [(x["Driver"]["familyName"], rname.get(int(r["round"]), r["raceName"].replace(" Grand Prix", ""))) for r in res for x in r["Results"] if x["position"] == "1"]
            podl = [x["Driver"]["familyName"] for r in res for x in r["Results"] if x["positionText"].isdigit() and int(x["positionText"]) <= 3]
            pods = len(podl)
            starts = {}
            for r in res:
                for x in r["Results"]:
                    starts[f1_name(x["Driver"])] = starts.get(f1_name(x["Driver"]), 0) + 1
            poles = sum(1 for r in q if any(x["position"] == "1" for x in r["QualifyingResults"]))
            sw = sum(1 for r in spr if any(x["position"] == "1" for x in r["SprintResults"]))
            team_total = next((c["points"] for c in doc["constructors"] if c["team"] == fav), None)
            if team_total is not None and sum(per.values()) != team_total:
                rep["warnings"].append("%s: driver points %s != constructors' table %s" % (fav, sum(per.values()), team_total))
            def grouped(pairs):
                g = {}
                for who, where in pairs:
                    g.setdefault(who, []).append(where)
                return "; ".join("%s: %s" % (w, ", ".join(v)) for w, v in g.items())
            pc = {}
            for who in podl:
                pc[who] = pc.get(who, 0) + 1
            most = max(starts.values()) if starts else 0
            fd["stats"] = [{"label": "Wins", "value": "%d%s" % (len(wins), " (%s)" % grouped(wins) if wins else "")},
                           {"label": "Podiums", "value": "%d%s" % (pods, " (%s)" % ", ".join("%s %d" % kv for kv in sorted(pc.items(), key=lambda kv: -kv[1])) if pods else "")},
                           {"label": "Poles", "value": str(poles)}, {"label": "Sprint wins", "value": str(sw)}] + \
                          [{"label": canon(d, known).split()[-1] + (" (%d races)" % starts.get(d, 0) if starts.get(d, 0) < most else ""), "value": "%s pts" % p}
                           for d, p in sorted(per.items(), key=lambda x: -x[1])]
            fd["statsNote"] = "Through the %s: %s points in all." % (last["name"], sum(per.values()))
        fd["counted"] = sorted(set(fd.get("counted", [])) | {last["name"]})
        fd["updatedAt"] = iso(now)
        dump(fd, os.path.join(out, slug + ".json"))
        rep.setdefault("changed_docs", []).append(slug)

def final(args):
    load_aliases(args.db)
    now = ts(args.now) if args.now else dt.datetime.now(UTC)
    favs = favorites(args.db)
    os.makedirs(args.out, exist_ok=True)
    rep = {"script": VERSION, "now": iso(now), "changed": [], "live": [], "newFinals": [], "warnings": [], "errors": []}
    for lg in (args.lg or "").split(","):
        if lg in ("nfl", "ncaafb", "mlb"):
            try:
                md = merged_db(args.db, args.out)
                if lg == "nfl":
                    nfl_standings(md, args.out, now, rep)
                if lg == "ncaafb":
                    cfb_standings(md, args.out, now, rep)
                    md = merged_db(args.db, args.out)
                    ranked_records(md, args.out, now, rep)
                md = merged_db(args.db, args.out)
                for ab in sorted(favs.get(lg, set())):
                    if lg == "mlb":
                        fav_baseball_add(md, args.out, now, rep, ab)
                    else:
                        st_doc = load(md, "nfl_standings" if lg == "nfl" else "cfb_standings") or {}
                        row = next((r for g in st_doc.get("groups", [{"rows": st_doc.get("rows", [])}]) for r in g["rows"] if r["abbr"] == ab), None)
                        rec = ("%d-%d" % (row["w"], row["l"]) + ("-%d" % row["t"] if row.get("t") else "")) if row else None
                        fav_football(md, args.out, now, rep, lg, ab, rec)
            except Exception as e:
                rep["errors"].append("%s final: %s" % (lg, e))
            continue
        try:
            {"nascar": nascar_final, "f1": f1_final}[lg](args.db, args.out, now, rep, favs)
        except KeyError:
            rep["errors"].append("no scripted final for %s" % lg)
        except Exception as e:
            rep["errors"].append("%s final: %s" % (lg, e))
    save_aliases(args.db, args.out, rep)
    rep["changed_docs"] = sorted(set(rep.get("changed_docs", [])))
    json.dump(rep, open(os.path.join(args.out, "report.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps(rep, ensure_ascii=False, indent=1))

# ---------------------------------------------------------------- favorite team season docs (football: rebuilt from every box score)
ESPN_TEAM_ID = {"ncaafb:ALA": "333", "ncaafb:UGA": "61"}

def espn_team_id(lg, abbr):
    key = "%s:%s" % (lg, abbr)
    if key in ESPN_TEAM_ID:
        return ESPN_TEAM_ID[key]
    rev = {v: k for k, v in ABBR.get(lg, {}).items()}
    want = rev.get(abbr, abbr)
    if lg == "nfl":
        return want.lower()
    teams = fetch("https://site.api.espn.com/apis/site/v2/sports/%s/teams?limit=1000" % PATHS[lg])
    for t in teams["sports"][0]["leagues"][0]["teams"]:
        if t["team"]["abbreviation"] == want:
            return t["team"]["id"]
    raise RuntimeError("no ESPN id for %s" % key)

def team_schedule(lg, abbr):
    tid = espn_team_id(lg, abbr)
    out = []
    for st in ("2", "3"):  # regular season and postseason
        try:
            d = fetch("https://site.api.espn.com/apis/site/v2/sports/%s/teams/%s/schedule?seasontype=%s" % (PATHS[lg], tid, st))
        except Exception:
            continue
        rec = (d.get("team") or {}).get("recordSummary")
        for e in d.get("events", []):
            c = e["competitions"][0]
            side = {}
            for t in c["competitors"]:
                sc = t.get("score")
                sc = sc.get("displayValue") if isinstance(sc, dict) else sc
                side[t["homeAway"]] = {"abbr": ABBR[lg].get(t["team"]["abbreviation"], t["team"]["abbreviation"]),
                                       "name": t["team"].get("displayName"), "score": num(sc) if sc not in (None, "") else None}
            status, detail = status_of(lg, c["status"])
            tv = ", ".join(b.get("media", {}).get("shortName", "") for b in c.get("broadcasts", []) if b.get("media"))
            out.append({"eid": e["id"], "start": ts(e["date"]), "status": status, "detail": detail, "away": side["away"],
                        "home": side["home"], "tv": tv, "post": st == "3", "tbd": bool(c.get("timeValid") is False),
                        "label": (c.get("notes") or [{}])[0].get("headline") if c.get("notes") else None, "record": rec})
    return out

def player_box_full(lg, eid, abbr):
    """Every player of one team in one game, plus that game's team totals."""
    s = fetch("https://site.api.espn.com/apis/site/v2/sports/%s/summary?event=%s" % (PATHS[lg], eid))
    teams = {}
    for t in s["boxscore"].get("teams", []):
        ab = ABBR[lg].get(t["team"]["abbreviation"], t["team"]["abbreviation"])
        teams[ab] = {x["name"]: x["displayValue"] for x in t.get("statistics", [])}
    players, sacks = {}, {}
    for blk in s["boxscore"].get("players", []):
        ab = ABBR[lg].get(blk["team"]["abbreviation"], blk["team"]["abbreviation"])
        cats = {}
        for c in blk.get("statistics", []):
            keys = c.get("keys", [])
            rows = []
            for at in c.get("athletes", []):
                dn = at["athlete"].get("displayName", "")
                if not dn.strip() or dn.strip() == "Team" or dn.startswith(" "):
                    continue
                rows.append((at["athlete"].get("shortName") or short(dn), dict(zip(keys, at.get("stats", [])))))
            cats[c["name"]] = rows
            if c["name"] == "defensive" and c.get("totals"):
                tot = dict(zip(keys, c["totals"]))
                sacks[ab] = num(tot.get("sacks", 0)) or 0
        players[ab] = cats
    return s, teams, players, sacks

def possec(v):
    try:
        m, s_ = v.split(":")
        return int(m) * 60 + int(s_)
    except Exception:
        return 0

def fav_football(db, out, now, rep, lg, abbr, standings_record=None):
    slug = "fav_%s_%s" % (lg, re.sub(r"[^A-Za-z0-9]+", "_", abbr))
    fd = load(db, slug) or {"key": "%s:%s" % (lg, abbr), "kind": "team", "lg": lg, "abbr": abbr, "games": []}
    sched = team_schedule(lg, abbr)
    if not sched:
        rep["errors"].append("%s: no schedule from ESPN" % slug)
        return
    used, games = set(), []
    old = fd.get("games", [])
    for e in sorted(sched, key=lambda e: e["start"]):
        g = None
        for o in old:
            if id(o) in used:
                continue
            if {o["away"]["abbr"], o["home"]["abbr"]} == {e["away"]["abbr"], e["home"]["abbr"]} and abs((ts(o["start"]) - e["start"]).total_seconds()) < 4 * 86400:
                g = o
                used.add(id(o))
                break
        if g is None:  # same day, opponent spelled differently
            for o in old:
                if id(o) not in used and abs((ts(o["start"]) - e["start"]).total_seconds()) < 36 * 3600:
                    g = o
                    used.add(id(o))
                    break
        if g is None:
            g = {"id": e["eid"], "away": dict(e["away"]), "home": dict(e["home"])}
        g["start"] = iso(e["start"]).replace("Z", "+00:00")
        g["status"] = e["status"]
        if e["detail"]:
            g["detail"] = e["detail"]
        else:
            g.pop("detail", None)
        for sd in ("away", "home"):
            g[sd]["abbr"], g[sd]["name"] = e[sd]["abbr"], e[sd]["name"] or g[sd].get("name")
            if e["status"] in ("live", "final") and e[sd]["score"] is not None:
                g[sd]["score"] = e[sd]["score"]
            elif e["status"] == "pre":
                g[sd].pop("score", None)
        if e["tv"] and not g.get("tv"):
            g["tv"] = e["tv"]
        if e["post"]:
            g["post"] = True
        if e["tbd"] and e["status"] == "pre":
            g["tbd"] = True
        else:
            g.pop("tbd", None)
        g["_eid"] = e["eid"]
        games.append(g)
    # rebuild totals from every final's box score
    TT = {"games": 0, "us": {}, "them": {}}
    S = {"passing": {}, "rushing": {}, "receiving": {}, "defensive": {}, "interceptions": {}, "kicking": {}}
    pending = []
    def add(d, k, v):
        d[k] = d.get(k, 0) + (num(v) if not isinstance(num(v), str) else 0)
    for g in games:
        if g["status"] != "final":
            continue
        try:
            s, teams, players, sacks = player_box_full(lg, g["_eid"], abbr)
        except Exception as ex:
            pending.append(g["id"])
            rep["warnings"].append("%s %s box: %s" % (slug, g["id"], ex))
            continue
        opp = g["home"]["abbr"] if g["away"]["abbr"] == abbr else g["away"]["abbr"]
        if abbr not in teams or abbr not in players:
            pending.append(g["id"])
            continue
        # game card box (lines/stats/plays/players for this favorite only), same as the league doc
        if not (g.get("lines") and g.get("players")):
            try:
                g.update(football_box(lg, g["_eid"], g, {abbr}))
            except Exception as ex:
                rep["warnings"].append("%s %s game box: %s" % (slug, g["id"], ex))
        TT["games"] += 1
        for side, ab in (("us", abbr), ("them", opp)):
            t = teams.get(ab, {})
            d = TT[side]
            add(d, "pts", g["away"]["score"] if g["away"]["abbr"] == ab else g["home"]["score"])
            add(d, "tot", t.get("totalYards", 0)); add(d, "pas", t.get("netPassingYards", 0)); add(d, "rus", t.get("rushingYards", 0))
            add(d, "to", t.get("turnovers", 0)); add(d, "fd", t.get("firstDowns", 0)); add(d, "possSec", possec(t.get("possessionTime", "0:00")))
            add(d, "sk", sacks.get(ab, 0))
            pen = (t.get("totalPenaltiesYards") or "0-0").split("-")
            add(d, "pen", pen[0]); add(d, "penYds", pen[1] if len(pen) > 1 else 0)
        P = players[abbr]
        for nm, v in P.get("passing", []):
            c, a = (v.get("completions/passingAttempts") or "0/0").split("/")
            d = S["passing"].setdefault(nm, {})
            add(d, "c", c); add(d, "a", a); add(d, "y", v.get("passingYards", 0)); add(d, "td", v.get("passingTouchdowns", 0)); add(d, "int", v.get("interceptions", 0))
        for nm, v in P.get("rushing", []):
            d = S["rushing"].setdefault(nm, {})
            add(d, "car", v.get("rushingAttempts", 0)); add(d, "y", v.get("rushingYards", 0)); add(d, "td", v.get("rushingTouchdowns", 0))
        for nm, v in P.get("receiving", []):
            d = S["receiving"].setdefault(nm, {})
            add(d, "rec", v.get("receptions", 0)); add(d, "y", v.get("receivingYards", 0)); add(d, "td", v.get("receivingTouchdowns", 0))
            d["long"] = max(d.get("long", -99), num(v.get("longReception", 0)) or 0)
        for nm, v in P.get("defensive", []):
            d = S["defensive"].setdefault(nm, {})
            add(d, "tot", v.get("totalTackles", 0)); add(d, "solo", v.get("soloTackles", 0)); add(d, "sack", v.get("sacks", 0)); add(d, "tfl", v.get("tacklesForLoss", 0))
        for nm, v in P.get("interceptions", []):
            if (num(v.get("interceptions", 0)) or 0) > 0:
                d = S["interceptions"].setdefault(nm, {})
                add(d, "int", v.get("interceptions", 0)); add(d, "y", v.get("interceptionYards", 0)); add(d, "td", v.get("interceptionTouchdowns", 0))
        for nm, v in P.get("kicking", []):
            fm, fa = (v.get("fieldGoalsMade/fieldGoalAttempts") or "0/0").split("/")
            xm, xa = (v.get("extraPointsMade/extraPointAttempts") or "0/0").split("/")
            d = S["kicking"].setdefault(nm, {})
            add(d, "fm", fm); add(d, "fa", fa); add(d, "xm", xm); add(d, "xa", xa); add(d, "pts", v.get("totalKickingPoints", 0))
            d["long"] = max(d.get("long", 0), num(v.get("longFieldGoalMade", 0)) or 0)
    n = TT["games"]
    f1d = lambda x: "%.1f" % (x / n) if n else "0.0"
    rows = []
    if n:
        u, t = TT["us"], TT["them"]
        mmss = lambda s_: "%d:%02d" % (s_ // n // 60, s_ // n % 60)
        rows = [{"label": "Points / game", "us": f1d(u["pts"]), "them": f1d(t["pts"])},
                {"label": "Total yards / game", "us": f1d(u["tot"]), "them": f1d(t["tot"])},
                {"label": "Passing yards / game", "us": f1d(u["pas"]), "them": f1d(t["pas"])},
                {"label": "Rushing yards / game", "us": f1d(u["rus"]), "them": f1d(t["rus"])},
                {"label": "First downs / game", "us": f1d(u["fd"]), "them": f1d(t["fd"])},
                {"label": "Turnovers", "us": str(u["to"]), "them": str(t["to"])},
                {"label": "Sacks", "us": str(u["sk"]), "them": str(t["sk"])},
                {"label": "Penalties", "us": "%d for %d yds" % (u["pen"], u["penYds"]), "them": "%d for %d yds" % (t["pen"], t["penYds"])},
                {"label": "Time of possession", "us": mmss(u["possSec"]), "them": mmss(t["possSec"])}]
    L = []
    pas = sorted(S["passing"].items(), key=lambda kv: -kv[1]["y"])
    if pas: L.append({"title": "Passing", "cols": ["C/ATT", "YDS", "TD", "INT"], "rows": [[k, "%d/%d" % (v["c"], v["a"]), v["y"], v["td"], v["int"]] for k, v in pas]})
    ru = sorted(S["rushing"].items(), key=lambda kv: -kv[1]["y"])[:6]
    if ru: L.append({"title": "Rushing", "cols": ["CAR", "YDS", "AVG", "TD"], "rows": [[k, v["car"], v["y"], "%.1f" % (v["y"] / v["car"]) if v["car"] else "0.0", v["td"]] for k, v in ru]})
    re_ = sorted(S["receiving"].items(), key=lambda kv: -kv[1]["y"])[:6]
    if re_: L.append({"title": "Receiving", "cols": ["REC", "YDS", "TD", "LONG"], "rows": [[k, v["rec"], v["y"], v["td"], v["long"]] for k, v in re_]})
    de = sorted(S["defensive"].items(), key=lambda kv: (-kv[1]["tot"], -kv[1]["sack"]))[:6]
    if de: L.append({"title": "Defense", "cols": ["TOT", "SOLO", "SACK", "TFL"], "rows": [[k, v["tot"], v["solo"], v["sack"], v["tfl"]] for k, v in de]})
    it = sorted(S["interceptions"].items(), key=lambda kv: (-kv[1]["int"], -kv[1]["y"]))
    if it: L.append({"title": "Interceptions", "cols": ["INT", "YDS", "TD"], "rows": [[k, v["int"], v["y"], v["td"]] for k, v in it]})
    ki = sorted(S["kicking"].items(), key=lambda kv: -kv[1]["pts"])
    if ki: L.append({"title": "Kicking", "cols": ["FG", "LONG", "XP", "PTS"], "rows": [[k, "%d/%d" % (v["fm"], v["fa"]), v["long"], "%d/%d" % (v["xm"], v["xa"]), v["pts"]] for k, v in ki]})
    # checks
    finals = [g for g in games if g["status"] == "final" and g["id"] not in pending]
    pts_sum = sum((g["away"]["score"] if g["away"]["abbr"] == abbr else g["home"]["score"]) for g in finals)
    if n and TT["us"]["pts"] != pts_sum:
        rep["errors"].append("%s: points total %s != sum of scores %s" % (slug, TT["us"]["pts"], pts_sum))
    for g in games:
        g.pop("_eid", None)
    fd["games"] = games
    fd["teamTotals"] = TT
    fd["seasonTotals"] = S
    fd["teamStats"] = {"oppLabel": "Opp", "rows": rows, "note": "Per game unless noted, from the box scores of all %d games. Sacks are made by each defense." % n}
    fd["leaders"] = L
    fd["leadersNote"] = "Season totals added up from the box scores of all %d games." % n + (" %d game box score%s not published yet." % (len(pending), "s" if len(pending) > 1 else "") if pending else "")
    fd["counted"] = [g["id"] for g in finals]
    if pending:
        fd["pending"] = {"box": pending}
    else:
        fd.pop("pending", None)
    rec = standings_record or next((e["record"] for e in sched if e.get("record") and not e["post"]), None)
    if rec:
        fd["record"] = rec
    fd["season"] = str(sched[0]["start"].year)
    fd["updatedAt"] = iso(now)
    dump(fd, os.path.join(out, slug + ".json"))
    rep.setdefault("changed_docs", []).append(slug)
    rep["changed"].append("%s: %d finals counted, record %s%s" % (slug, n, fd.get("record"), ", pending %s" % pending if pending else ""))

# ---------------------------------------------------------------- standings and the AP poll
def team_name_map(db):
    m = {}
    for d in ("nfl_standings", "cfb_standings", "mlb_standings", "cfb_rankings"):
        doc = load(db, d) or {}
        for g in doc.get("groups", [{"rows": doc.get("rows", [])}]):
            for r in g.get("rows", []):
                m[r["abbr"]] = r
    return m

def nfl_standings(db, out, now, rep):
    doc = load(db, "nfl_standings") or {}
    old = {r["abbr"]: r for g in doc.get("groups", []) for r in g["rows"]}
    d = fetch("https://site.api.espn.com/apis/v2/sports/football/nfl/standings")
    groups = []
    for c in d["children"]:
        conf = "AFC" if "American" in c["name"] else "NFC"
        rows = []
        for e in c["standings"]["entries"]:
            st = {s["name"]: s.get("displayValue") for s in e["stats"]}
            ab = ABBR["nfl"].get(e["team"]["abbreviation"], e["team"]["abbreviation"])
            r = {"rank": int(st.get("playoffSeed") or 99), "abbr": ab, "name": e["team"]["displayName"],
                 "div": (old.get(ab) or {}).get("div", ""), "w": int(st.get("wins", 0)), "l": int(st.get("losses", 0))}
            t = int(float(st.get("ties", 0) or 0))
            if t:
                r["t"] = t
            rows.append(r)
        rows.sort(key=lambda r: r["rank"])
        groups.append({"name": conf, "cut": 7, "rows": rows})
    groups.sort(key=lambda g: g["name"])
    return finish_standings(db, out, now, rep, "nfl_standings", "nfl", doc, {"groups": groups})

def cfb_standings(db, out, now, rep):
    doc = load(db, "cfb_standings") or {}
    d = fetch("https://site.api.espn.com/apis/v2/sports/football/college-football/standings?group=8")
    rows = []
    for e in d["standings"]["entries"]:
        ov, cf = None, None
        for s in e["stats"]:
            if s["name"] == "overall":
                ov = s["displayValue"]
            if s["name"] == "vs. Conf.":
                cf = s["displayValue"]
        w, l = [int(x) for x in ov.split("-")[:2]]
        r = {"abbr": ABBR["ncaafb"].get(e["team"]["abbreviation"], e["team"]["abbreviation"]), "name": e["team"]["displayName"], "w": w, "l": l}
        if cf:
            r["conf"] = cf
        rows.append(r)
    def key(r):
        cw, cl = [int(x) for x in r.get("conf", "0-0").split("-")[:2]]
        cp = cw / (cw + cl) if cw + cl else 0
        return (-cp, -cw, -(r["w"] / max(1, r["w"] + r["l"])), -r["w"])
    rows.sort(key=key)
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return finish_standings(db, out, now, rep, "cfb_standings", "ncaafb", doc,
                            {"title": "SEC standings", "rows": rows,
                             "note": "Sorted by SEC record, then overall record. Conference record shown when available."})

def finish_standings(db, out, now, rep, docid, lg, old, new):
    """Check the new table against every final in the league doc before accepting it."""
    league = load(db, lg) or {"games": []}
    counted = set(old.get("counted", []))
    finals = [g for g in league["games"] if g["status"] == "final"]
    rows_new = {r["abbr"]: r for g in new.get("groups", [{"rows": new.get("rows", [])}]) for r in g["rows"]}
    rows_old = {r["abbr"]: r for g in old.get("groups", [{"rows": old.get("rows", [])}]) for r in g["rows"]}
    lag = []
    for g in finals:
        if g["id"] in counted:
            continue
        for sd, other in (("away", "home"), ("home", "away")):
            ab = g[sd]["abbr"]
            if ab not in rows_old or ab not in rows_new:
                continue
            o, n_ = rows_old[ab], rows_new[ab]
            if (n_["w"] + n_["l"] + n_.get("t", 0)) <= (o["w"] + o["l"] + o.get("t", 0)):
                lag.append(g["id"])
    if lag:
        rep["warnings"].append("%s: source has not caught up with %s; kept stored table" % (docid, sorted(set(lag))))
        return False
    new["counted"] = [g["id"] for g in finals if any(g[s]["abbr"] in rows_new for s in ("away", "home"))]
    new["updatedAt"] = iso(now)
    dump(new, os.path.join(out, docid + ".json"))
    rep.setdefault("changed_docs", []).append(docid)
    rep["changed"].append("%s refreshed (%d finals counted)" % (docid, len(new["counted"])))
    return True

def ap_poll(db, out, now, rep):
    doc = load(db, "cfb_rankings") or {}
    r = fetch("https://site.api.espn.com/apis/site/v2/sports/football/college-football/rankings")
    ap = next((x for x in r.get("rankings", []) if x.get("type") == "ap" or "AP" in x.get("name", "")), None)
    if not ap:
        rep["warnings"].append("AP poll not found")
        return False
    m = re.search(r"Week (\d+)", ap.get("headline", ""))
    wk = int(m[1]) if m else None
    old_wk = re.search(r"Week (\d+)", doc.get("note", ""))
    old_wk = int(old_wk[1]) if old_wk else 0
    pdate = ts(ap.get("date"))
    if wk is None or wk <= old_wk:
        rep["changed"].append("AP poll: stored %s (week %s), no newer poll yet" % (doc.get("date"), old_wk))
        return False
    old_rows = {x["abbr"]: x for x in doc.get("rows", [])}
    sec = {"ALA", "ARK", "AUB", "FLA", "UGA", "UK", "LSU", "MSST", "MIZZ", "OKLA", "MISS", "SCAR", "TENN", "TEX", "TXAM", "VAN"}
    rows, need_conf = [], []
    for x in ap["ranks"]:
        t = x["team"]
        ab = ABBR["ncaafb"].get(t.get("abbreviation"), t.get("abbreviation"))
        row = {"rank": x["current"], "abbr": ab, "name": "%s %s" % (t.get("location", ""), t.get("name", "")), "record": x.get("recordSummary")}
        if ab not in sec:
            if old_rows.get(ab, {}).get("conf"):
                row["conf"] = old_rows[ab]["conf"]
            else:
                need_conf.append(ab)
        rows.append(row)
    if len(rows) != 25:
        rep["warnings"].append("AP poll has %d rows" % len(rows))
    # Sunday date in Eastern time
    sun = pdate.astimezone(ET)
    league = load(db, "ncaafb") or {"games": []}
    new = {"poll": "AP Top 25", "date": sun.strftime("%b ") + str(sun.day), "note": "Week %d poll." % wk, "rows": rows,
           "since": iso(pdate), "counted": [g["id"] for g in league["games"] if g["status"] == "final" and ts(g["start"]) < pdate],
           "updatedAt": iso(now)}
    dump(new, os.path.join(out, "cfb_rankings.json"))
    rep.setdefault("changed_docs", []).append("cfb_rankings")
    rep["changed"].append("AP poll: stored %s (week %d)" % (new["date"], wk))
    if need_conf:
        rep["needConf"] = need_conf
    return True

def ranked_records(db, out, now, rep):
    """Between polls: keep each ranked team's record current with the finals in the ncaafb doc."""
    doc = load(db, "cfb_rankings")
    league = load(db, "ncaafb")
    if not doc or not league:
        return
    counted = set(doc.get("counted", []))
    changed = False
    for g in league["games"]:
        if g["status"] != "final" or g["id"] in counted:
            continue
        a, h = g["away"], g["home"]
        for me, opp in ((a, h), (h, a)):
            row = next((r for r in doc["rows"] if r["abbr"] == me["abbr"]), None)
            if not row:
                continue
            parts = [int(x) for x in row["record"].split("-")]
            if me["score"] > opp["score"]:
                parts[0] += 1
            else:
                parts[1] += 1
            row["record"] = "-".join(str(x) for x in parts)
        if any(r["abbr"] in (a["abbr"], h["abbr"]) for r in doc["rows"]):
            counted.add(g["id"])
            changed = True
    if changed:
        doc["counted"] = sorted(counted)
        doc["updatedAt"] = iso(now)
        dump(doc, os.path.join(out, "cfb_rankings.json"))
        rep.setdefault("changed_docs", []).append("cfb_rankings")
        rep["changed"].append("cfb_rankings: records updated")

# ---------------------------------------------------------------- cadence
SEC = {"ALA", "ARK", "AUB", "FLA", "UGA", "UK", "LSU", "MSST", "MIZZ", "OKLA", "MISS", "SCAR", "TENN", "TEX", "TXAM", "VAN"}
RIVALRY = {frozenset(("ALA", "AUB")): "Iron Bowl", frozenset(("ALA", "TENN")): "Third Saturday in October",
           frozenset(("TEX", "OKLA")): "Red River Rivalry", frozenset(("UGA", "FLA")): "World's Largest Outdoor Cocktail Party",
           frozenset(("MISS", "MSST")): "Egg Bowl", frozenset(("UGA", "AUB")): "Deep South's Oldest Rivalry",
           frozenset(("LSU", "ARK")): "Battle for the Golden Boot", frozenset(("TEX", "TXAM")): "Lone Star Showdown",
           frozenset(("OSU", "MICH")): "The Game", frozenset(("OKLA", "OKST")): "Bedlam", frozenset(("PITT", "WVU")): "Backyard Brawl",
           frozenset(("UGA", "GT")): "Clean, Old-Fashioned Hate", frozenset(("FLA", "FSU")): "Sunshine Showdown",
           frozenset(("SCAR", "CLEM")): "Palmetto Bowl", frozenset(("UK", "LOU")): "Governor's Cup", frozenset(("TENN", "VAN")): "Tennessee–Vanderbilt rivalry",
           frozenset(("MIZZ", "ARK")): "Battle Line Rivalry", frozenset(("LSU", "TXAM")): "LSU–Texas A&M rivalry", frozenset(("ALA", "LSU")): "Alabama–LSU rivalry"}
GAME_HOURS = {"nfl": 3.5, "ncaafb": 4.0, "mlb": 3.5}

def next_slot(now, minutes=10, allowed=None):
    """Next :x5 slot strictly after now (allowed = set of minutes past the hour, e.g. {25, 55})."""
    t = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    for _ in range(24 * 60):
        if t.minute % 10 == 5 and (allowed is None or t.minute in allowed):
            if allowed is not None or (t - now).total_seconds() >= 60:
                return t
        t += dt.timedelta(minutes=1)
    return None

def slot_before(t):
    """Latest :x5 slot at or up to 10 minutes before t."""
    t = t.replace(second=0, microsecond=0)
    for k in range(0, 11):
        c = t - dt.timedelta(minutes=k)
        if c.minute % 10 == 5:
            return c
    return t

def activity(db, now):
    """What is live now, what is about to start, and the next start time, from the stored docs."""
    favs = favorites(db)
    fav_live, sport_live, upcoming = [], set(), []
    for lg in ("nfl", "ncaafb", "mlb"):
        doc = load(db, lg) or {"games": []}
        fav = favs.get(lg, set())
        for g in doc["games"]:
            st = ts(g["start"])
            isf = g["away"]["abbr"] in fav or g["home"]["abbr"] in fav
            running = g["status"] == "live" or (g["status"] == "pre" and st <= now + dt.timedelta(minutes=10) and st >= now - dt.timedelta(hours=GAME_HOURS[lg] + 2))
            if running:
                sport_live.add(lg)
                if isf and lg != "mlb":
                    fav_live.append("%s %s@%s" % (lg, g["away"]["abbr"], g["home"]["abbr"]))
            elif g["status"] == "pre" and st > now:
                upcoming.append((st, lg, isf))
    u = load(db, "ufc") or {"events": []}
    for ev in u["events"]:
        st = ts(ev["start"])
        if ev["status"] == "live" or (ev["status"] == "upcoming" and st <= now + dt.timedelta(minutes=10)):
            sport_live.add("ufc")
        elif ev["status"] == "upcoming" and st > now:
            upcoming.append((st, "ufc", False))
    for lg, hours in (("nascar", 6), ("f1", 4), ("moto", 4)):
        d = load(db, lg) or {}
        nx = d.get("next") or {}
        if not nx.get("start"):
            continue
        st = ts(nx["start"])
        isf = bool(favs.get(lg) or (lg == "f1" and favs.get("f1team")))
        if nx.get("status") == "live" or (st <= now + dt.timedelta(minutes=10) and now <= st + dt.timedelta(hours=hours)):
            sport_live.add(lg)
            if isf:
                fav_live.append("%s %s" % (lg, nx.get("name")))
        elif st > now:
            upcoming.append((st, lg, isf))
    return fav_live, sport_live, sorted(upcoming)

def plan_next_wake(db, now):
    fav_live, sport_live, upcoming = activity(db, now)
    soon = [u for u in upcoming if u[0] <= now + dt.timedelta(minutes=12)]
    if fav_live or any(u[2] and u[1] != "mlb" for u in soon):
        return next_slot(now), "10-minute favorite updates (%s)" % ", ".join(fav_live or [u[1] for u in soon])
    if sport_live - {"mlb"} or any(u[1] != "mlb" for u in soon):
        return next_slot(now, allowed={25, 55}), "30-minute sport updates (%s)" % ", ".join(sorted(sport_live) or [u[1] for u in soon])
    if "mlb" in sport_live:
        favs = favorites(db)
        doc = load(db, "mlb") or {"games": []}
        fav_mlb = any(g["status"] == "live" and (g["away"]["abbr"] in favs.get("mlb", set()) or g["home"]["abbr"] in favs.get("mlb", set())) for g in doc["games"])
        if fav_mlb:
            return next_slot(now, allowed={25, 55}), "30-minute updates for a favorite MLB game"
        return next_slot(now, allowed={55}), "hourly MLB updates"
    if upcoming:
        st = upcoming[0][0]
        return max(slot_before(st), next_slot(now)), "next start: %s at %s" % (upcoming[0][1], iso(st))
    return None, "nothing scheduled"

def full_sports_for_slot(db, now):
    """Leagues that get a full refresh on this run: live sports at :25/:55, MLB only at :55."""
    _, sport_live, upcoming = activity(db, now)
    m = (now.minute + 2) // 5 * 5 % 60  # tolerate a run that starts a minute or two late
    out = set()
    if m in (25, 55):
        out |= {s for s in sport_live if s in ("nfl", "ncaafb", "ufc")}
    if m == 55 and "mlb" in sport_live:
        out.add("mlb")
    return out

# ---------------------------------------------------------------- morning planner
def plan_team(db, out, now, rep, lg, favs, ranked):
    doc = load(db, lg) or {"games": []}
    et = now.astimezone(ET)
    monday = (et - dt.timedelta(days=et.weekday())).date()
    start_d = min(monday, (et - dt.timedelta(days=4)).date())
    days = [(start_d + dt.timedelta(days=i)) for i in range(((et.date() + dt.timedelta(days=8)) - start_d).days + 1)]
    try:
        evs = scoreboard(lg, {d.strftime("%Y%m%d") for d in days})
    except Exception as e:
        rep["errors"].append("%s schedule: %s" % (lg, e))
        return
    evs = [e for e in evs if "/" not in e["away"]["abbr"] + e["home"]["abbr"]]  # skip matchups not set yet
    fav = favs.get(lg, set())
    def keep_ev(e):
        a, h = e["away"]["abbr"], e["home"]["abbr"]
        if a in fav or h in fav:
            return True
        if lg == "ncaafb":
            return a in SEC or h in SEC or a in ranked or h in ranked or (e["away"]["rank"] or 99) <= 25 or (e["home"]["rank"] or 99) <= 25
        return True
    used = set()
    games, added, moved = [], 0, 0
    old = sorted(doc["games"], key=lambda g: ts(g["start"]))
    for g in old:
        e = match(g, evs, used, lg)
        if e:
            if e["status"] == "pre" and g["status"] == "pre" and e["start"] != ts(g["start"]):
                g["start"] = iso(e["start"]).replace("Z", "+00:00")
                moved += 1
            if g["status"] in ("pre", "live") or e["status"] != g["status"]:
                flip = e["away"]["abbr"] != g["away"]["abbr"]
                ea, eh = (e["home"], e["away"]) if flip else (e["away"], e["home"])
                if e["status"] in ("live", "final") and ea["score"] is not None:
                    g["away"]["score"], g["home"]["score"] = ea["score"], eh["score"]
                    g["status"] = e["status"]
                    if e["detail"]:
                        g["detail"] = e["detail"]
                    g["seen"] = iso(now)
                elif e["status"] == "ppd":
                    g["status"] = "ppd"
                    g["detail"] = e["detail"] or "Postponed"
            if e["tv"]:
                g["tv"] = e["tv"]
            if e["venue"] and not g.get("venue"):
                g["venue"] = e["venue"]
            if e.get("label") and not g.get("label"):
                g["label"] = e["label"]
        elif g["status"] == "pre" and any(({e["away"]["abbr"], e["home"]["abbr"]} & {g["away"]["abbr"], g["home"]["abbr"]}) and abs((e["start"] - ts(g["start"])).total_seconds()) < 36 * 3600 for e in evs):
            rep["warnings"].append("%s %s@%s on %s: no exact match on ESPN but one of the teams plays that day; kept as stored" % (lg, g["away"]["abbr"], g["home"]["abbr"], g["start"][:10]))
        elif g["status"] == "pre" and dt.datetime.combine(days[0], dt.time(0), ET) <= ts(g["start"]) <= dt.datetime.combine(days[-1], dt.time(23, 59), ET):
            rep["changed"].append("%s: dropped %s@%s on %s (no longer on the schedule)" % (lg, g["away"]["abbr"], g["home"]["abbr"], g["start"][:10]))
            continue
        games.append(g)
    for e in evs:
        if e["eid"] in used or not keep_ev(e):
            continue
        g = {"id": e["eid"], "status": e["status"], "start": iso(e["start"]).replace("Z", "+00:00"),
             "away": {"abbr": e["away"]["abbr"], "name": e["away"]["name"]}, "home": {"abbr": e["home"]["abbr"], "name": e["home"]["name"]}}
        if e["status"] in ("live", "final") and e["away"]["score"] is not None:
            g["away"]["score"], g["home"]["score"] = e["away"]["score"], e["home"]["score"]
            g["seen"] = iso(now)
        if e["detail"]:
            g["detail"] = e["detail"]
        if e["tv"]:
            g["tv"] = e["tv"]
        if e["venue"]:
            g["venue"] = e["venue"]
        if e.get("label"):
            g["label"] = e["label"]
        if lg == "ncaafb":
            r = RIVALRY.get(frozenset((e["away"]["abbr"], e["home"]["abbr"])))
            if r:
                g["label"], g["rivalry"] = r, True
        games.append(g)
        added += 1
    # window: finals since Monday (and the last 4 days), live, pre within 8 days; favorites keep their last final and next game
    lo = dt.datetime.combine(start_d, dt.time(0), ET)
    hi = now + dt.timedelta(days=8)
    keep = []
    for g in games:
        st = ts(g["start"])
        inwin = g["status"] == "live" or (g["status"] in ("final", "ppd") and st >= lo) or (g["status"] == "pre" and st <= hi and st >= now - dt.timedelta(days=2))
        if inwin:
            keep.append(g)
    for ab in fav:
        mine = [g for g in games if ab in (g["away"]["abbr"], g["home"]["abbr"])]
        finals = [g for g in mine if g["status"] == "final"]
        pres = [g for g in mine if g["status"] == "pre" and ts(g["start"]) > now]
        for g in ([max(finals, key=lambda g: ts(g["start"]))] if finals else []) + ([min(pres, key=lambda g: ts(g["start"]))] if pres else []):
            if g not in keep:
                keep.append(g)
    keep.sort(key=lambda g: ts(g["start"]))
    for g in keep:
        if not (g["away"]["abbr"] in fav or g["home"]["abbr"] in fav):
            for k in ("lines", "stats", "plays", "players"):
                g.pop(k, None)
    doc["games"] = keep
    doc["updatedAt"] = iso(now)
    dump(doc, os.path.join(out, lg + ".json"))
    rep.setdefault("changed_docs", []).append(lg)
    rep["changed"].append("%s schedule: %d games kept, %d added, %d start times changed" % (lg, len(keep), added, moved))

def plan_ufc(db, out, now, rep):
    doc = load(db, "ufc") or {"events": []}
    sbd = fetch("https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard")
    cal = sbd["leagues"][0].get("calendar", [])
    events = {e["name"]: e for e in doc["events"]}
    for c in cal:
        if not isinstance(c, dict) or "Contender" in c.get("label", "") or not c.get("label", "").startswith("UFC"):
            continue
        st = ts(c["startDate"])
        if not (now - dt.timedelta(days=21) <= st <= now + dt.timedelta(days=22)):
            continue
        eid = re.search(r"events/(\d+)", c["event"]["$ref"])[1]
        ev = events.get(c["label"]) or next((e for e in doc["events"] if last_name(e["name"]) == last_name(c["label"]) and abs((ts(e["start"]) - st).total_seconds()) < 3 * 86400), None)
        if ev and ev.get("status") == "final" and all(b.get("method") or b.get("winner") for b in ev["bouts"]):
            continue
        try:
            fc = ufc_card(eid)
        except Exception as e:
            rep["warnings"].append("ufc card %s: %s" % (c["label"], e))
            continue
        bouts, probs = ufc_bouts(fc, (ev or {}).get("bouts", []))
        if not bouts:
            continue
        if not ev:
            ev = {"name": c["label"], "status": "upcoming"}
            doc["events"].append(ev)
            rep["changed"].append("ufc: added %s" % c["label"])
        ev["bouts"] = bouts
        starts = [ts(b["start"]) for b in bouts if b.get("start")]
        if starts and ev.get("status") == "upcoming":
            ev["start"] = iso(min(starts)).replace("Z", "+00:00")
        if not ev.get("venue") and fc.get("venue", {}).get("displayNameLocation"):
            ev["venue"] = fc["venue"]["displayNameLocation"]
        bc = [b for card in fc.get("cards", {}).values() for b in card.get("broadcasts", [])]
        if bc and not ev.get("tv"):
            ev["tv"] = bc[0].get("media", {}).get("shortName")
        done = sum(1 for b in bouts if b.get("method"))
        if done == len(bouts):
            ev["status"] = "final"
    finals = sorted([e for e in doc["events"] if e["status"] == "final"], key=lambda e: ts(e["start"]), reverse=True)
    keep_f = set(id(e) for e in finals[:4]) | set(id(e) for e in finals if ts(e["start"]) >= now - dt.timedelta(days=now.astimezone(ET).weekday() + 1))
    doc["events"] = sorted([e for e in doc["events"] if e["status"] != "final" or id(e) in keep_f], key=lambda e: ts(e["start"]))
    doc["events"] = [e for e in doc["events"] if e["status"] != "upcoming" or ts(e["start"]) <= now + dt.timedelta(days=22)]
    doc["updatedAt"] = iso(now)
    dump(doc, os.path.join(out, "ufc.json"))
    rep.setdefault("changed_docs", []).append("ufc")
    rep["changed"].append("ufc: %d events (%s)" % (len(doc["events"]), ", ".join("%s %s" % (e["name"].split(":")[-1].strip(), e["status"]) for e in doc["events"])))

def plan_nascar(db, out, now, rep):
    doc = load(db, "nascar")
    if not doc:
        return
    nx = doc.get("next") or {}
    if not nx.get("start") or nx.get("status") == "live":
        return
    st = ts(nx["start"])
    race = nascar_race_for(nx, st.year)
    if not race:
        return
    changed = False
    rs = nascar_start(race)
    if rs != st:
        nx["start"] = rs.isoformat(); nx["date"] = date_text(rs); changed = True
    if race.get("television_broadcaster") and not nx.get("tv"):
        nx["tv"] = nascar_tv(race["television_broadcaster"]); changed = True
    if now >= st - dt.timedelta(days=3):
        try:
            w = fetch("https://cf.nascar.com/cacher/%d/1/%d/weekend-feed.json" % (st.year, race["race_id"]))["weekend_race"][0]
            res = [r for r in w.get("results", []) if r.get("starting_position")]
            if res and len(set(r["starting_position"] for r in res)) == len(res) and min(r["starting_position"] for r in res) == 1:
                known = [r["driver"] for r in doc.get("standings", [])]
                grid = [{"pos": r["starting_position"], "driver": canon(clean_driver(r["driver_fullname"]), known), "team": "No. %s" % r["car_number"]}
                        for r in sorted(res, key=lambda r: r["starting_position"])]
                if grid != nx.get("grid"):
                    nx["grid"] = grid
                    nx["gridNote"] = "Official NASCAR starting lineup."
                    changed = True
                    rep["changed"].append("nascar: starting lineup stored (%d cars, pole %s)" % (len(grid), grid[0]["driver"]))
        except Exception as e:
            rep["warnings"].append("nascar lineup: %s" % e)
    nx.pop("quali", None)
    if changed:
        doc["updatedAt"] = iso(now)
        dump(doc, os.path.join(out, "nascar.json"))
        rep.setdefault("changed_docs", []).append("nascar")

def plan_f1(db, out, now, rep):
    doc = load(db, "f1")
    if not doc:
        return
    nx = doc.get("next") or {}
    if not nx.get("start") or nx.get("status") == "live":
        return
    st = ts(nx["start"])
    races = jol("%d/races.json?limit=100" % st.year)["RaceTable"]["Races"]
    rstart = lambda r: ts("%sT%s" % (r["date"], r.get("time", "12:00:00Z")))
    rnd = min(races, key=lambda r: abs((rstart(r) - st).total_seconds()))
    changed = False
    if rstart(rnd) != st:
        nx["start"] = iso(rstart(rnd)).replace("Z", "+00:00"); nx["date"] = date_text(rstart(rnd).astimezone(ET)); changed = True
    if now >= st - dt.timedelta(days=3):
        q = jol("%d/%s/qualifying.json?limit=30" % (st.year, rnd["round"]))["RaceTable"]["Races"]
        if q and q[0].get("QualifyingResults"):
            known = [r["driver"] for r in doc.get("standings", [])]
            grid = [{"pos": int(x["position"]), "driver": canon(f1_name(x["Driver"]), known), "team": f1_team(x["Constructor"]["name"])} for x in q[0]["QualifyingResults"]]
            if grid != nx.get("grid"):
                nx["grid"] = grid
                nx["gridNote"] = "Provisional grid from qualifying; grid penalties may change it before the start."
                changed = True
                rep["changed"].append("f1: qualifying order stored (pole %s)" % grid[0]["driver"])
    nx.pop("quali", None)
    if changed:
        doc["updatedAt"] = iso(now)
        dump(doc, os.path.join(out, "f1.json"))
        rep.setdefault("changed_docs", []).append("f1")

def plan(args):
    load_aliases(args.db)
    now = ts(args.now) if args.now else dt.datetime.now(UTC)
    favs = favorites(args.db)
    os.makedirs(args.out, exist_ok=True)
    rep = {"script": VERSION, "now": iso(now), "changed": [], "live": [], "newFinals": [], "warnings": [], "errors": []}
    ranked = {r["abbr"] for r in (load(args.db, "cfb_rankings") or {}).get("rows", [])}
    steps = [("nfl", lambda: plan_team(args.db, args.out, now, rep, "nfl", favs, ranked)),
             ("ncaafb", lambda: plan_team(args.db, args.out, now, rep, "ncaafb", favs, ranked)),
             ("mlb", lambda: plan_team(args.db, args.out, now, rep, "mlb", favs, ranked)),
             ("ufc", lambda: plan_ufc(args.db, args.out, now, rep)),
             ("nascar", lambda: plan_nascar(args.db, args.out, now, rep)),
             ("f1", lambda: plan_f1(args.db, args.out, now, rep))]
    et = now.astimezone(ET)
    if et.weekday() in (0, 6):  # Sunday afternoon is handled by ticks; Monday morning is the fallback
        steps.append(("ap", lambda: ap_poll(args.db, args.out, now, rep)))
    for name, fn in steps:
        try:
            fn()
        except Exception as e:
            rep["errors"].append("%s: %s" % (name, e))
    # read the docs we just wrote for the rest of the plan
    merged = os.path.join(args.out, "_merged")
    os.makedirs(merged, exist_ok=True)
    for f in os.listdir(args.db):
        if f.endswith(".json"):
            src = os.path.join(args.out, f) if os.path.exists(os.path.join(args.out, f)) else os.path.join(args.db, f)
            open(os.path.join(merged, f), "w").write(open(src).read())
    for lg in ("nfl", "ncaafb"):
        for ab in sorted(favs.get(lg, set())):
            try:
                fav_football(merged, args.out, now, rep, lg, ab)
            except Exception as e:
                rep["errors"].append("fav %s:%s: %s" % (lg, ab, e))
    for f in os.listdir(args.out):
        if f.endswith(".json") and f != "report.json":
            open(os.path.join(merged, f), "w").write(open(os.path.join(args.out, f)).read())
    wake, why = plan_next_wake(merged, now)
    rep["nextWake"] = iso(wake) if wake else None
    rep["nextWakeWhy"] = why
    _, _, upcoming = activity(merged, now)
    rep["upcoming"] = ["%s %s%s" % (iso(u[0]), u[1], " (favorite)" if u[2] else "") for u in upcoming[:12]]
    save_aliases(args.db, args.out, rep)
    rep["changed_docs"] = sorted(set(rep.get("changed_docs", [])))
    json.dump(rep, open(os.path.join(args.out, "report.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps(rep, ensure_ascii=False, indent=1))


def merged_db(db, out):
    md = os.path.join(out, "_merged")
    os.makedirs(md, exist_ok=True)
    for f in set(os.listdir(db)) | set(x for x in os.listdir(out) if x.endswith(".json")):
        if not f.endswith(".json") or f == "report.json":
            continue
        src = os.path.join(out, f) if os.path.exists(os.path.join(out, f)) else os.path.join(db, f)
        open(os.path.join(md, f), "w").write(open(src).read())
    return md

def fav_baseball_add(db, out, now, rep, abbr):
    """Copy the favorite's finished MLB games (with box scores) from the league doc into its season doc."""
    slug = "fav_mlb_" + abbr
    fd = load(db, slug)
    lg = load(db, "mlb") or {"games": []}
    st = load(db, "mlb_standings") or {}
    if not fd:
        rep["warnings"].append("%s missing; the final wrap must build it" % slug)
        return
    counted = set(fd.get("counted", []))
    changed = False
    for g in lg["games"]:
        if g["status"] != "final" or abbr not in (g["away"]["abbr"], g["home"]["abbr"]) or g["id"] in counted:
            continue
        same = [x for x in fd["games"] if {x["away"]["abbr"], x["home"]["abbr"]} == {g["away"]["abbr"], g["home"]["abbr"]}
                and abs((ts(x["start"]) - ts(g["start"])).total_seconds()) < 12 * 3600]
        if same and same[0]["id"] in counted:
            continue
        new = json.loads(json.dumps(g))
        if same:
            new["id"] = same[0]["id"]
            for k in ("post", "label"):
                if same[0].get(k):
                    new[k] = same[0][k]
            fd["games"][fd["games"].index(same[0])] = new
        else:
            fd["games"].append(new)
        counted.add(new["id"])
        changed = True
    if changed:
        fd["games"].sort(key=lambda x: ts(x["start"]))
        fd["counted"] = sorted(counted)
        rec = next((r for g_ in st.get("groups", []) for r in g_["rows"] if r["abbr"] == abbr), None)
        if rec:
            fd["record"] = "%d-%d" % (rec["w"], rec["l"])
        fd["updatedAt"] = iso(now)
        dump(fd, os.path.join(out, slug + ".json"))
        rep.setdefault("changed_docs", []).append(slug)
        rep["changed"].append("%s: finals added" % slug)

# ---------------------------------------------------------------- tick
def lines_ok(lines, game):
    try:
        for side in ("away", "home"):
            tot = sum(x for x in lines[side] if isinstance(x, (int, float)))
            if tot != game[side]["score"]:
                return False
        return True
    except Exception:
        return False

def tick(args):
    load_aliases(args.db)
    now = ts(args.now) if args.now else dt.datetime.now(UTC)
    nowi = iso(now)
    full = set(x for x in (args.sports or "").split(",") if x and x != "auto")
    if args.sports == "auto":
        full = full_sports_for_slot(args.db, now)
    favs = favorites(args.db)
    os.makedirs(args.out, exist_ok=True)
    rep = {"script": VERSION, "now": nowi, "changed": [], "live": [], "newFinals": [], "warnings": [], "errors": []}
    for lg in ("nfl", "ncaafb", "mlb"):
        doc = load(args.db, lg)
        fav = favs.get(lg, set())
        if not doc:
            continue
        def wanted(g):
            return lg in full or g["away"]["abbr"] in fav or g["home"]["abbr"] in fav
        targets = []
        for g in doc["games"]:
            if not wanted(g):
                continue
            st = ts(g["start"])
            due = g["status"] in ("pre", "live") and st <= now + dt.timedelta(minutes=30) and st >= now - dt.timedelta(days=5)
            is_fav = g["away"]["abbr"] in fav or g["home"]["abbr"] in fav
            needbox = is_fav and g["status"] == "final" and not (g.get("lines") and g.get("players"))
            if due or needbox:
                targets.append(g)
        if not targets:
            continue
        dates = set(ts(g["start"]).astimezone(ET).strftime("%Y%m%d") for g in targets)
        try:
            evs = scoreboard(lg, dates)
        except Exception as e:
            rep["errors"].append("%s scoreboard: %s" % (lg, e))
            continue
        changed = False
        used = set()
        for g in sorted(targets, key=lambda g: ts(g["start"])):
            e = match(g, evs, used, lg)
            if not e:
                rep["warnings"].append("%s %s %s@%s not on ESPN scoreboard; kept stored copy" % (lg, g["id"], g["away"]["abbr"], g["home"]["abbr"]))
                continue
            was = {"status": g["status"], "detail": g.get("detail"), "a": g["away"].get("score"), "h": g["home"].get("score")}
            flip = e["away"]["abbr"] != g["away"]["abbr"]
            ea, eh = (e["home"], e["away"]) if flip else (e["away"], e["home"])
            g["status"] = e["status"]
            if e["detail"]:
                g["detail"] = e["detail"]
            else:
                g.pop("detail", None)
            if e["status"] == "pre":
                g["away"].pop("score", None); g["home"].pop("score", None)
                if e["start"] != ts(g["start"]):
                    g["start"] = iso(e["start"]).replace("Z", "+00:00")
            elif e["status"] in ("live", "final"):
                if ea["score"] is None or eh["score"] is None:
                    rep["errors"].append("%s %s: source has no score; kept stored copy" % (lg, g["id"]))
                    g.update({"status": was["status"]})
                    continue
                g["away"]["score"], g["home"]["score"] = ea["score"], eh["score"]
                g.pop("wp", None)
            g["seen"] = nowi
            if e["tv"] and not g.get("tv"):
                g["tv"] = e["tv"]
            if e["venue"] and not g.get("venue"):
                g["venue"] = e["venue"]
            is_fav = g["away"]["abbr"] in fav or g["home"]["abbr"] in fav
            if is_fav and (g["status"] == "live" or (g["status"] == "final" and (was["status"] != "final" or not (g.get("lines") and g.get("players"))))):
                try:
                    if lg == "mlb":
                        box, probs = mlb_box(g, fav)
                        rep["warnings"] += probs
                    else:
                        box = football_box(lg, e["eid"], g, fav)
                    if lines_ok(box["lines"], g):
                        g.update(box)
                    else:
                        g.update({k: v for k, v in box.items() if k != "lines"})
                        rep["warnings"].append("%s %s: line scores %s don't add up to %s-%s yet; kept stored lines" % (lg, g["id"], box["lines"], g["away"]["score"], g["home"]["score"]))
                except Exception as ex:
                    rep["errors"].append("%s %s box score: %s" % (lg, g["id"], ex))
            now_ = {"status": g["status"], "detail": g.get("detail"), "a": g["away"].get("score"), "h": g["home"].get("score")}
            line = "%s %s %s-%s %s, %s (was %s %s)" % (lg, g["away"]["abbr"], now_["a"], now_["h"], g["home"]["abbr"], now_["detail"] or now_["status"], was["status"], was["detail"] or "")
            rep["changed"].append(line)
            if g["status"] == "live":
                rep["live"].append(line)
            if g["status"] == "final" and was["status"] != "final":
                rep["newFinals"].append({"lg": lg, "id": g["id"], "away": g["away"]["abbr"], "home": g["home"]["abbr"], "fav": is_fav})
            changed = True
        for g in doc["games"]:  # box scores only on favorites' games
            if not (g["away"]["abbr"] in fav or g["home"]["abbr"] in fav):
                for k in ("lines", "stats", "plays", "players"):
                    g.pop(k, None)
        if changed:
            doc["updatedAt"] = nowi
            dump(doc, os.path.join(args.out, lg + ".json"))
            rep["changed_docs"] = rep.get("changed_docs", []) + [lg]
    for name, fn, on in [("ufc", lambda: ufc_tick(args.db, args.out, now, rep), "ufc" in full),
                         ("nascar", lambda: nascar_tick(args.db, args.out, now, rep, favs), "nascar" in full or favs.get("nascar")),
                         ("f1", lambda: f1_tick(args.db, args.out, now, rep), "f1" in full or favs.get("f1") or favs.get("f1team")),
                         ("moto", lambda: moto_tick(args.db, args.out, now, rep), "moto" in full or favs.get("moto"))]:
        if on:
            try:
                fn()
            except Exception as e:
                rep["errors"].append("%s: %s" % (name, e))
    et = now.astimezone(ET)
    if et.weekday() == 6 and et.hour in (15, 16) and 20 <= et.minute <= 35:  # Sunday 3:25 and 4:25 PM: new AP poll?
        try:
            ap_poll(args.db, args.out, now, rep)
        except Exception as e:
            rep["errors"].append("ap poll: %s" % e)
    save_aliases(args.db, args.out, rep)
    rep["fullRefresh"] = sorted(full)
    rep["anyLive"] = bool(rep["live"])
    md = merged_db(args.db, args.out)
    wake, why = plan_next_wake(md, now)
    rep["nextWake"] = iso(wake) if wake else None
    rep["nextWakeWhy"] = why
    rep["changed_docs"] = sorted(set(rep.get("changed_docs", [])))
    json.dump(rep, open(os.path.join(args.out, "report.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps(rep, ensure_ascii=False, indent=1))

def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    t = sp.add_parser("tick")
    t.add_argument("--db", required=True); t.add_argument("--out", required=True)
    t.add_argument("--sports", default=""); t.add_argument("--now")
    f = sp.add_parser("final")
    f.add_argument("--db", required=True); f.add_argument("--out", required=True)
    f.add_argument("--lg", required=True); f.add_argument("--now")
    p = sp.add_parser("plan")
    p.add_argument("--db", required=True); p.add_argument("--out", required=True); p.add_argument("--now")
    a = ap.parse_args()
    if a.cmd == "plan":
        plan(a)
    if a.cmd == "tick":
        tick(a)
    elif a.cmd == "final":
        final(a)

if __name__ == "__main__":
    main()
