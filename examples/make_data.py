"""Generate the demo datasets. Synthetic, deterministic, no real data.

    python examples/make_data.py            # writes examples/data/*.csv
"""

from __future__ import annotations

import csv
import datetime as dt
import math
import random
from pathlib import Path

OUT = Path(__file__).parent / "data"
END = dt.date(2026, 10, 15)


def write(name: str, header: list[str], rows: list[list]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / name, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
    print(f"wrote {OUT / name} ({len(rows):,} rows)")


def poisson(rng: random.Random, lam: float) -> int:
    if lam > 50:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= l:
            return k
        k += 1


# ---------------------------------------------------------------- saas.csv
SYL = ["ac", "me", "lu", "na", "vo", "ra", "zen", "qu", "ix", "tor", "bri", "sto", "kai", "pel", "dro", "fy", "lo", "mi", "sa", "ter", "nex", "ori", "pha", "glo"]
SUFFIX = ["Labs", "AI", "HQ", "Cloud", "Systems", "Health", "Pay", "Works", "Analytics", "Robotics", "Studio", "Logistics"]
PLANS = [("free", 0.52, 0, 0.11), ("starter", 0.27, 29, 0.055), ("pro", 0.16, 99, 0.032), ("enterprise", 0.05, 0, 0.012)]
COUNTRIES = [("US", 38), ("IN", 14), ("UK", 10), ("DE", 9), ("CA", 6), ("FR", 5), ("BR", 5), ("AU", 4), ("JP", 3), ("SG", 3), ("NL", 3)]
CHANNELS = [("organic", 34), ("paid_search", 18), ("referral", 14), ("content", 16), ("outbound", 8), ("product_hunt", 4), ("partner", 6)]


def company(rng: random.Random, used: set[str]) -> str:
    while True:
        name = "".join(rng.choice(SYL) for _ in range(rng.choice([2, 2, 3]))).capitalize()
        name = f"{name} {rng.choice(SUFFIX)}"
        if name not in used:
            used.add(name)
            return name


def weighted(rng: random.Random, pairs):
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def make_saas(rng: random.Random) -> None:
    start = END - dt.timedelta(days=548)  # ~18 months
    rows, used, cid = [], set(), 1000
    days = (END - start).days + 1
    for d in range(days):
        day = start + dt.timedelta(days=d)
        growth = 5 + 11 * (d / days) ** 1.3
        weekday = 0.62 if day.weekday() >= 5 else 1.0
        launch = 1 + 4.5 * math.exp(-abs(d - 290) / 3)  # Product Hunt launch
        n = poisson(rng, growth * weekday * launch)
        for _ in range(n):
            cid += 1
            r = rng.random()
            acc = 0.0
            for plan, share, price, churn in PLANS:
                acc += share
                if r <= acc:
                    break
            seats = 1
            if plan == "pro":
                seats = rng.choice([1, 1, 2, 3, 3, 5, 8])
                mrr = price * seats if seats < 3 else round(price * seats * 0.85)
            elif plan == "enterprise":
                seats = rng.randint(15, 220)
                mrr = int(round(max(900, seats * rng.uniform(18, 32)), -1))
            else:
                mrr = price
            channel = "product_hunt" if launch > 2 and rng.random() < 0.6 else weighted(rng, CHANNELS)
            # churn: monthly hazard, slightly lower for newer cohorts (product got better)
            hazard = churn * (1.15 - 0.3 * d / days)
            churned_at = ""
            t = day
            while True:
                t += dt.timedelta(days=30)
                if t > END:
                    break
                if rng.random() < hazard:
                    churned_at = (t - dt.timedelta(days=rng.randint(0, 29))).isoformat()
                    break
            rows.append([cid, company(rng, used), day.isoformat(), plan, seats, mrr,
                         weighted(rng, COUNTRIES), channel, churned_at])
    write("saas.csv", ["customer_id", "company", "signup_date", "plan", "seats", "mrr", "country", "channel", "churned_at"], rows)


# --------------------------------------------------------- github_stars.csv
def make_github(rng: random.Random) -> None:
    start = END - dt.timedelta(days=364)
    rows = []
    open_issues = 0.0
    for d in range(365):
        day = start + dt.timedelta(days=d)
        base = 8 + 30 * (d / 365) ** 1.5
        hn = 1600 * math.exp(-(d - 117) / 2.2) if d >= 117 else 0       # Show HN front page
        release = 520 * math.exp(-(d - 230) / 3.5) if d >= 230 else 0   # v1.0 release
        tweet = 260 * math.exp(-(d - 301) / 1.5) if d >= 301 else 0     # viral tweet
        weekday = 0.75 if day.weekday() >= 5 else 1.0
        stars = poisson(rng, (base + hn + release + tweet) * weekday)
        forks = poisson(rng, stars * 0.065 + 0.5)
        opened = poisson(rng, 0.8 + stars / 45 + d / 120)
        closed = min(int(open_issues + opened), poisson(rng, 0.7 + open_issues * 0.09 + d / 150))
        open_issues = max(0.0, open_issues + opened - closed)
        prs = poisson(rng, (1.0 + d / 110) * weekday)
        views = poisson(rng, 140 + stars * 21 + d * 2.2)
        uniques = round(views * rng.uniform(0.31, 0.4))
        if hn > base:
            ref = "news.ycombinator.com"
        elif tweet > base:
            ref = "x.com"
        elif release > base:
            ref = rng.choice(["x.com", "reddit.com", "news.ycombinator.com"])
        else:
            ref = rng.choices(["github.com", "google.com", "x.com", "reddit.com", "dev.to"], [38, 30, 16, 10, 6])[0]
        rows.append([day.isoformat(), stars, forks, opened, closed, prs, views, uniques, ref])
    write("github_stars.csv", ["date", "new_stars", "new_forks", "issues_opened", "issues_closed", "prs_merged", "page_views", "unique_visitors", "top_referrer"], rows)


# -------------------------------------------------------------- x_posts.csv
FORMATS = [("text", 34, 1.0, 0.018), ("thread", 12, 1.7, 0.026), ("image", 20, 1.35, 0.022),
           ("video", 10, 2.1, 0.017), ("link", 14, 0.45, 0.009), ("poll", 10, 0.95, 0.03)]
TOPICS = [("ai", 30, 1.35), ("startups", 22, 1.1), ("coding", 20, 1.0), ("career", 14, 0.9), ("memes", 14, 1.25)]
HOUR_W = [1, 1, 1, 1, 1, 2, 4, 7, 9, 10, 9, 7, 6, 6, 5, 5, 6, 7, 9, 10, 9, 6, 3, 2]


def hour_effect(h: int) -> float:
    return 0.55 + 0.9 * math.exp(-((h - 9.5) ** 2) / 6) + 0.75 * math.exp(-((h - 20) ** 2) / 5)


def make_x(rng: random.Random) -> None:
    start = dt.date(2026, 1, 1)
    days = (END - start).days + 1
    rows, pid = [], 1789000000000000000
    for d in range(days):
        day = start + dt.timedelta(days=d)
        for _ in range(poisson(rng, 2.6 if day.weekday() < 5 else 1.4)):
            pid += rng.randint(10**12, 10**13)
            hour = rng.choices(range(24), HOUR_W)[0]
            ts = dt.datetime.combine(day, dt.time(hour, rng.randint(0, 59)))
            fmt, _, fmult, eng = weighted_full(rng, FORMATS)
            topic, _, tmult = weighted_full(rng, TOPICS)
            followers = 4200 * (1 + d / 95) ** 1.35
            mu = math.log(followers * 0.55 * fmult * tmult * hour_effect(hour))
            views = int(rng.lognormvariate(mu, 0.85))
            if rng.random() < 0.012:
                views *= rng.randint(8, 25)  # the occasional banger
            rate = eng * rng.uniform(0.6, 1.5)
            likes = poisson(rng, views * rate)
            replies = poisson(rng, likes * (0.16 if fmt in ("poll", "thread") else 0.09))
            reposts = poisson(rng, likes * 0.11)
            bookmarks = poisson(rng, likes * (0.45 if fmt == "thread" else 0.14))
            profile_visits = poisson(rng, views * 0.006)
            follows = poisson(rng, profile_visits * 0.09)
            chars = {"text": rng.randint(40, 280), "thread": rng.randint(900, 3800), "poll": rng.randint(30, 160)}.get(fmt, rng.randint(20, 200))
            rows.append([pid, ts.isoformat(sep=" "), ts.strftime("%a"), hour, fmt, topic, chars,
                         views, likes, replies, reposts, bookmarks, profile_visits, follows])
    write("x_posts.csv", ["post_id", "posted_at", "weekday", "hour", "format", "topic", "chars", "views", "likes",
                          "replies", "reposts", "bookmarks", "profile_visits", "new_followers"], rows)


def weighted_full(rng: random.Random, items):
    return rng.choices(items, weights=[i[1] for i in items])[0]


if __name__ == "__main__":
    make_saas(random.Random(7))
    make_github(random.Random(42))
    make_x(random.Random(2026))
