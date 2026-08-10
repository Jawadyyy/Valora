"""Pure-logic tests. No network, no mocks. Run: python test_valorant_bot.py"""

import ast
import json
import os
import tempfile
import pathlib

import valorant_bot_simple as bot


def _use_tmp_users_file():
    """Point the module at a throwaway users file, return its path."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)  # start missing
    bot.USERS_FILE = pathlib.Path(path)
    return pathlib.Path(path)


# --- load_users -----------------------------------------------------------

def test_load_users_missing():
    p = _use_tmp_users_file()
    assert bot.load_users() == {}, "missing file -> {}"
    assert not p.exists()


def test_load_users_corrupt():
    p = _use_tmp_users_file()
    p.write_text("{not valid json", encoding="utf-8")
    assert bot.load_users() == {}, "corrupt file -> {}"
    p.unlink()


# --- save_users round-trip ------------------------------------------------

def test_save_users_roundtrip():
    p = _use_tmp_users_file()
    users = {"123": {"ssid": "cookie", "access_token": "a", "puuid": "x"}}
    bot.save_users(users)
    assert bot.load_users() == users, "round-trip mismatch"
    # no leftover temp file
    assert not p.with_suffix(".json.tmp").exists()
    p.unlink()


def test_no_password_in_code():
    """Regression guard: cookie reauth must never accept or store a password.

    Two precise checks - no function takes a password argument, and no dict
    literal has a password key. Prose mentioning the word is fine and expected.
    """
    tree = ast.parse(pathlib.Path(bot.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.arg):
            assert "password" not in node.arg.lower(), \
                f"password parameter at line {node.lineno}: {node.arg}"
        elif isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    assert "password" not in k.value.lower(), \
                        f"password dict key at line {node.lineno}"


# --- access_token parsing -------------------------------------------------

def test_token_from_uri():
    uri = "https://playvalorant.com/opt_in#access_token=TOK123&id_token=ID&expires_in=3600"
    assert bot.token_from_uri(uri) == "TOK123"


def test_token_from_uri_missing():
    assert bot.token_from_uri("") is None
    assert bot.token_from_uri("https://playvalorant.com/opt_in") is None
    # dead ssid -> Riot bounces to the login page, no fragment
    assert bot.token_from_uri("https://auth.riotgames.com/login") is None
    # explicit error redirect
    assert bot.token_from_uri("https://playvalorant.com/opt_in#error=access_denied") is None


# --- cookie parsing -------------------------------------------------------

SSID = "eyJhbGciOi.PAYLOAD.SIG"


def test_parse_cookies_full_header():
    """The normal case: a whole request `cookie:` header."""
    raw = f"tdid=TD; did=DI; ssid={SSID}; clid=eu1; csid=CS"
    got = bot.parse_cookies(raw)
    assert got == {"tdid": "TD", "did": "DI", "ssid": SSID,
                   "clid": "eu1", "csid": "CS"}, got


def test_parse_cookies_keeps_all_names():
    """All cookies must survive - reauth with the full set lasts ~3 weeks
    against ~1 week for ssid alone."""
    got = bot.parse_cookies(f"tdid=TD; ssid={SSID}; clid=eu1")
    assert set(got) == {"tdid", "ssid", "clid"}


def test_parse_cookies_bare_ssid():
    """A user who grabbed only the ssid value still gets something usable."""
    assert bot.parse_cookies(SSID) == {"ssid": SSID}
    assert bot.parse_cookies(f"  {SSID}  ") == {"ssid": SSID}


def test_parse_cookies_wrapping():
    assert bot.parse_cookies(f"cookie: ssid={SSID}") == {"ssid": SSID}
    assert bot.parse_cookies(f'"ssid={SSID}"') == {"ssid": SSID}


def test_parse_cookies_drops_set_cookie_attrs():
    """Pasting a Set-Cookie line must not turn Path/Domain/Secure into cookies."""
    raw = f"ssid={SSID}; Path=/; Domain=auth.riotgames.com; Secure; HttpOnly"
    assert bot.parse_cookies(raw) == {"ssid": SSID}


def test_parse_cookies_empty():
    assert bot.parse_cookies("") == {}
    assert bot.parse_cookies("   ") == {}


def test_parse_cookies_value_with_equals():
    """Base64 values contain '=' padding - only split on the first one."""
    got = bot.parse_cookies("ssid=abc==; tdid=xy=")
    assert got == {"ssid": "abc==", "tdid": "xy="}, got


# --- store parsing --------------------------------------------------------

STOREFRONT = {
    "SkinsPanelLayout": {
        "SingleItemOffers": ["uuid-a", "uuid-b"],
        "SingleItemStoreOffers": [
            {"OfferID": "uuid-a", "Cost": {bot.VP_UUID: 1775}},
            {"OfferID": "uuid-b", "Cost": {bot.VP_UUID: 2175}},
        ],
    }
}


def test_parse_store():
    offers = bot.parse_store(STOREFRONT)
    assert offers == [
        {"uuid": "uuid-a", "price": 1775},
        {"uuid": "uuid-b", "price": 2175},
    ], offers


def test_parse_store_empty():
    assert bot.parse_store({}) == []
    assert bot.parse_store({"SkinsPanelLayout": {}}) == []


def test_parse_store_missing_price():
    sf = {"SkinsPanelLayout": {"SingleItemStoreOffers": [{"OfferID": "x", "Cost": {}}]}}
    assert bot.parse_store(sf) == [{"uuid": "x", "price": None}]


# --- night market parsing -------------------------------------------------

NIGHT_MARKET = {
    "BonusStore": {
        "BonusStoreOffers": [
            {
                "Offer": {"OfferID": "nm-a", "Cost": {bot.VP_UUID: 1775}},
                "DiscountCosts": {bot.VP_UUID: 887},
                "DiscountPercent": 50,
            },
            {
                "Offer": {"OfferID": "nm-b", "Cost": {bot.VP_UUID: 2175}},
                "DiscountCosts": {bot.VP_UUID: 1522},
                "DiscountPercent": 30,
            },
        ]
    }
}


def test_parse_night_market():
    items = bot.parse_night_market(NIGHT_MARKET)
    assert items == [
        {"uuid": "nm-a", "original": 1775, "discounted": 887, "percent": 50},
        {"uuid": "nm-b", "original": 2175, "discounted": 1522, "percent": 30},
    ], items


def test_parse_night_market_inactive():
    assert bot.parse_night_market({}) == []
    assert bot.parse_night_market({"SkinsPanelLayout": {}}) == []


def test_discount_percent_from_api_not_computed():
    # percent must be the API value verbatim, even if it doesn't match the math.
    sf = {"BonusStore": {"BonusStoreOffers": [
        {"Offer": {"OfferID": "x", "Cost": {bot.VP_UUID: 1000}},
         "DiscountCosts": {bot.VP_UUID: 700}, "DiscountPercent": 47}
    ]}}
    assert bot.parse_night_market(sf)[0]["percent"] == 47


# --- region detection -----------------------------------------------------

def _pas_jwt(affinity):
    """Build a fake PAS token: header.payload.sig with the given affinity claim."""
    import base64 as _b64
    def enc(d):
        raw = json.dumps(d).encode()
        return _b64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    body = {"affinity": affinity} if affinity is not None else {}
    return f"{enc({'alg':'HS256'})}.{enc(body)}.sig"


def test_region_from_pas_maps_affinity_to_shard():
    # affinity "asia" -> shard "ap" is the mapping that was silently wrong before
    assert bot.region_from_pas(_pas_jwt("asia")) == "ap"
    assert bot.region_from_pas(_pas_jwt("ap")) == "ap"
    assert bot.region_from_pas(_pas_jwt("eu")) == "eu"
    assert bot.region_from_pas(_pas_jwt("na")) == "na"
    assert bot.region_from_pas(_pas_jwt("kr")) == "kr"
    # latam/br collapse onto the na shard
    assert bot.region_from_pas(_pas_jwt("latam")) == "na"
    assert bot.region_from_pas(_pas_jwt("br")) == "na"
    # SEA affinities live on the ap shard - pd.sea2 doesn't exist
    assert bot.region_from_pas(_pas_jwt("sea2")) == "ap"
    assert bot.region_from_pas(_pas_jwt("sea")) == "ap"


def test_region_from_pas_unknown_passes_through():
    """An affinity we don't have mapped shouldn't crash - use it verbatim."""
    assert bot.region_from_pas(_pas_jwt("newshard")) == "newshard"


def test_region_from_pas_malformed():
    assert bot.region_from_pas("not-a-jwt") is None
    assert bot.region_from_pas("") is None
    assert bot.region_from_pas(_pas_jwt(None)) is None, "no affinity claim -> None"


# --- token refresh persistence --------------------------------------------

def test_persist_tokens_matches_on_puuid():
    _use_tmp_users_file()
    bot.save_users({
        "discord-1": {"cookies": {"ssid": "old"}, "puuid": "PU-1",
                      "access_token": "old-a", "entitlements_token": "old-e"},
        "discord-2": {"cookies": {"ssid": "keep"}, "puuid": "PU-2",
                      "access_token": "keep-a", "entitlements_token": "keep-e"},
    })
    # simulate a refreshed user dict (puuid stable, tokens + rotated cookies new)
    bot._persist_tokens({"puuid": "PU-1", "access_token": "new-a",
                         "entitlements_token": "new-e",
                         "cookies": {"ssid": "new", "clid": "eu1"}})

    users = bot.load_users()
    assert users["discord-1"]["access_token"] == "new-a"
    assert users["discord-1"]["entitlements_token"] == "new-e"
    # rotated cookies must be written back, or the link dies in a few weeks
    assert users["discord-1"]["cookies"] == {"ssid": "new", "clid": "eu1"}
    # the other user is untouched
    assert users["discord-2"]["access_token"] == "keep-a"
    assert users["discord-2"]["cookies"] == {"ssid": "keep"}


def test_relogin_preserves_alerts():
    """Re-linking with a fresh cookie must not wipe alerts or the daily opt-in.

    Mirrors the merge the /login command does.
    """
    _use_tmp_users_file()
    bot.save_users({"d1": {"cookies": {"ssid": "old"}, "puuid": "PU-1",
                           "alerts": ["Oni Phantom"], "daily_shop": True}})
    users = bot.load_users()
    entry = users.get("d1", {})
    entry.update({"cookies": {"ssid": "new"}, "access_token": "a",
                  "entitlements_token": "e", "puuid": "PU-1"})
    users["d1"] = entry
    bot.save_users(users)

    after = bot.load_users()["d1"]
    assert after["cookies"] == {"ssid": "new"}
    assert after["alerts"] == ["Oni Phantom"], "alerts wiped on re-login"
    assert after["daily_shop"] is True


# --- static-data helpers --------------------------------------------------

import valorant_static as vs


def test_best_match_exact_over_substring():
    items = [{"displayName": "Reaver Vandal"}, {"displayName": "Vandal"}]
    assert vs.best_match("vandal", items)["displayName"] == "Vandal", "exact wins"


def test_best_match_shortest_substring():
    items = [{"displayName": "Reaver Vandal"}, {"displayName": "Prime Vandal Xe"}]
    # no exact 'vandal' -> shortest name containing it
    assert vs.best_match("vandal", items)["displayName"] == "Reaver Vandal"


def test_best_match_none():
    assert vs.best_match("zzz", [{"displayName": "Vandal"}]) is None
    assert vs.best_match("", [{"displayName": "Vandal"}]) is None


def test_tier_color_parses_rgb():
    assert vs.tier_color({"highlightColor": "00958733"}) == 0x009587
    assert vs.tier_color({"highlightColor": "zznothex"}) == vs.RED
    assert vs.tier_color({"highlightColor": ""}) == vs.RED
    assert vs.tier_color(None) == vs.RED


def test_fmt_damage():
    out = vs.fmt_damage([{"rangeStartMeters": 0, "rangeEndMeters": 15,
                          "headDamage": 160, "bodyDamage": 40, "legDamage": 34}])
    assert "0-15m" in out and "160 / 40 / 34" in out
    assert vs.fmt_damage([]) == "—"


def test_make_options_contains_answer_and_is_bounded():
    items = [{"displayName": n} for n in ["Vandal", "Phantom", "Sheriff", "Ghost", "Odin"]]
    opts = vs.make_options(items, "Vandal", n=4)
    assert "Vandal" in opts, "correct answer must be present"
    assert len(opts) == 4
    assert len(set(opts)) == len(opts), "no duplicate options"


def test_make_options_small_pool():
    items = [{"displayName": "Vandal"}, {"displayName": "Phantom"}]
    opts = vs.make_options(items, "Vandal", n=4)
    assert "Vandal" in opts and len(opts) == 2, "can't invent options beyond the pool"


def test_apply_result_scoring_and_streak():
    scores = {}
    vs.apply_result(scores, "u1", "Jinx", True)
    vs.apply_result(scores, "u1", "Jinx", True)
    e = vs.apply_result(scores, "u1", "Jinx", False)  # miss resets streak
    assert e["correct"] == 2 and e["attempts"] == 3
    assert e["streak"] == 0 and e["best_streak"] == 2
    vs.apply_result(scores, "u1", "Jinx", True)
    assert scores["u1"]["streak"] == 1 and scores["u1"]["best_streak"] == 2


def test_leaderboard_orders_by_points():
    scores = {
        "a": {"name": "A", "correct": 5, "attempts": 10, "best_streak": 3},
        "b": {"name": "B", "correct": 8, "attempts": 8, "best_streak": 8},
        "c": {"name": "C", "correct": 5, "attempts": 5, "best_streak": 5},
    }
    rows = vs.leaderboard_rows(scores)
    assert [r["name"] for r in rows] == ["B", "C", "A"], "points desc, tie broken by streak"
    assert rows[0]["accuracy"] == 100 and rows[2]["accuracy"] == 50


def test_pick_loadout_skips_missing_and_empty():
    weapons = [
        {"displayName": "Vandal", "skins": [{"displayName": "Reaver Vandal"},
                                            {"displayName": "Standard Vandal"}]},
        {"displayName": "Ghost", "skins": []},  # no skins -> skipped
    ]
    picks = vs.pick_loadout(weapons, ["Vandal", "Ghost", "Nonexistent"])
    assert len(picks) == 1 and picks[0][0] == "Vandal"
    # prefers a non-Standard skin when one exists
    assert picks[0][1] == "Reaver Vandal"


# --- henrik helpers -------------------------------------------------------

import henrik as hk


def test_parse_rank_full():
    data = {"current": {"tier": {"name": "Immortal 2"}, "rr": 43, "elo": 1943},
            "peak": {"tier": {"name": "Radiant"}, "season": {"short": "e7a2"}}}
    r = hk.parse_rank(data)
    assert r["tier"] == "Immortal 2" and r["rr"] == 43
    assert r["peak"] == "Radiant" and r["peak_season"] == "e7a2"


def test_parse_rank_unranked():
    r = hk.parse_rank({})
    assert r["tier"] == "Unranked" and r["rr"] is None and r["peak"] is None


def test_vp_to_pkr():
    hk.VP_TO_PKR = 1.30
    assert hk.vp_to_pkr(6700) == "~Rs 8,710"
    assert hk.vp_to_pkr(0) == "—"
    assert hk.vp_to_pkr(None) == "—"


def test_fmt_seconds():
    assert hk.fmt_seconds(807173) == "9d 8h"
    assert hk.fmt_seconds(7200) == "2h"
    assert hk.fmt_seconds(0) == "unknown"
    assert hk.fmt_seconds(None) == "unknown"


def test_valid_region():
    assert hk.valid_region("EU") == "eu"
    assert hk.valid_region("ap") == "ap"
    assert hk.valid_region("nonsense") == hk.DEFAULT_REGION
    assert hk.valid_region("") == hk.DEFAULT_REGION


def test_en_picks_english():
    items = [{"locale": "ar_AE", "content": "arabic"},
             {"locale": "en_US", "content": "english"}]
    assert hk.en(items) == "english"
    assert hk.en([{"locale": "de_DE", "content": "only-de"}]) == "only-de"
    assert hk.en([]) is None


def _match(kills, deaths, assists, head, body, leg, score, team, red, blue, mode="Competitive"):
    return {"meta": {"mode": mode}, "teams": {"red": red, "blue": blue},
            "stats": {"kills": kills, "deaths": deaths, "assists": assists,
                      "shots": {"head": head, "body": body, "leg": leg},
                      "score": score, "team": team}}


def test_aggregate_matches():
    matches = [
        _match(17, 18, 6, 14, 35, 3, 5376, "Blue", 13, 11),  # loss, 24 rounds
        _match(20, 10, 5, 20, 20, 0, 6000, "Red", 13, 8),    # win,  21 rounds
    ]
    a = hk.aggregate_matches(matches)
    assert a["games"] == 2
    assert a["kd"] == round(37 / 28, 2)
    assert a["kda"] == "37/28/11"
    assert a["hs"] == round(100 * 34 / 92)   # 34 head / 92 total shots
    assert a["acs"] == round(11376 / 45)     # total score / total rounds
    assert a["wl"] == "1-1"


def test_aggregate_matches_filters_competitive():
    matches = [
        _match(40, 5, 2, 30, 10, 0, 8000, "Blue", 13, 0, mode="Deathmatch"),
        _match(15, 15, 4, 10, 20, 2, 4000, "Blue", 11, 13, mode="Competitive"),
    ]
    a = hk.aggregate_matches(matches, competitive_only=True)
    assert a["games"] == 1 and a["kda"] == "15/15/4", "deathmatch should be excluded"


def test_aggregate_matches_empty():
    assert hk.aggregate_matches([]) is None


# --- esports helpers ------------------------------------------------------

import esports as es


def _ematch(state, teams, league="VCT Americas", mid="1", date="2026-08-09T21:00:00+00:00"):
    return {"date": date, "state": state, "league": {"name": league, "identifier": league.lower()},
            "match": {"id": mid, "teams": teams}}


def _team(name, won=False, wins=0):
    return {"name": name, "has_won": won, "game_wins": wins}


def test_filter_matches_state_and_league():
    ms = [
        _ematch("completed", [_team("A"), _team("B")], "VCT EMEA"),
        _ematch("unstarted", [_team("C"), _team("D")], "VCT Americas"),
        _ematch("in_progress", [_team("E"), _team("F")], "VCT Pacific"),
    ]
    assert len(es.filter_matches(ms, state="completed")) == 1
    assert len(es.filter_matches(ms, state=["unstarted", "in_progress"])) == 2
    assert len(es.filter_matches(ms, league="americas")) == 1
    assert len(es.filter_matches(ms, league="vct")) == 3


def test_fmt_score_bolds_winner():
    m = _ematch("completed", [_team("Sentinels", won=True, wins=2), _team("GIANTX", wins=0)])
    s = es.fmt_score(m)
    assert "**Sentinels**" in s and "2–0" in s and "GIANTX" in s
    assert "**GIANTX**" not in s, "loser not bold"


def test_new_results_for_dedupes_and_matches_team():
    ms = [
        _ematch("completed", [_team("Sentinels", won=True, wins=2), _team("100T")], mid="m1"),
        _ematch("completed", [_team("G2"), _team("Fnatic")], mid="m2"),
        _ematch("unstarted", [_team("Sentinels"), _team("NRG")], mid="m3"),
    ]
    fresh = es.new_results_for(ms, ["sentinels"], seen=[])
    assert [ (x['match']['id']) for x in fresh ] == ["m1"], "only completed, followed, unseen"
    # once m1 is seen, nothing new
    assert es.new_results_for(ms, ["sentinels"], seen=["m1"]) == []


def test_match_ts_bad_date():
    assert es.match_ts({"date": "garbage"}) == 0
    assert es.match_ts({}) == 0
    assert es.match_ts({"date": "2026-08-09T21:00:00+00:00"}) > 0


# --- rank roles -----------------------------------------------------------

import rankroles as rr


def test_base_tier():
    assert rr.base_tier("Platinum 2") == "Platinum"
    assert rr.base_tier("Radiant") == "Radiant"
    assert rr.base_tier("Immortal 3") == "Immortal"
    assert rr.base_tier("Unranked") is None
    assert rr.base_tier(None) is None
    assert rr.base_tier("") is None


def test_tier_names_cover_all_ranks():
    # every real Valorant tier's first word must map to a role
    for full in ["Iron 1", "Bronze 3", "Silver 2", "Gold 1", "Platinum 3",
                 "Diamond 2", "Ascendant 1", "Immortal 3", "Radiant"]:
        assert rr.base_tier(full) is not None, full


# --- stats: per-game history + RR sparkline -------------------------------

import stats as stx


def test_format_match_win():
    # shape verified live against Henrik stored-matches
    m = {"meta": {"map": {"name": "Split"}, "mode": "Competitive"},
         "stats": {"team": "Red", "character": {"name": "Raze"}, "score": 7178,
                   "kills": 25, "deaths": 18, "assists": 6,
                   "shots": {"head": 7, "body": 65, "leg": 4}},
         "teams": {"red": 13, "blue": 11}}
    f = stx.format_match(m)
    assert f["map"] == "Split" and f["agent"] == "Raze"
    assert f["kda"] == "25/18/6"
    assert f["acs"] == round(7178 / 24)       # score / (13+11) rounds
    assert f["hs"] == round(100 * 7 / 76)     # head / (head+body+leg)
    assert f["result"] == "W" and f["score"] == "13-11"


def test_format_match_loss_and_missing():
    # Blue team that lost; and no map/agent/shots -> safe defaults, no raise
    m = {"meta": {}, "stats": {"team": "Blue"}, "teams": {"red": 13, "blue": 5}}
    f = stx.format_match(m)
    assert f["result"] == "L" and f["score"] == "5-13"
    assert f["map"] == "?" and f["agent"] == "?"
    assert f["kda"] == "0/0/0" and f["hs"] == 0 and f["acs"] == 0


def test_sparkline_empty():
    assert stx.sparkline([]) == ""


def test_sparkline_single():
    assert stx.sparkline([42]) == "▁"      # one value -> lowest bar, no div-by-zero


def test_sparkline_flat():
    assert stx.sparkline([10, 10, 10]) == "▁▁▁"   # no range -> all lowest


def test_sparkline_normal():
    # 8 bars, scaled min..max: 0->▁, 50->▅ (round(3.5)=4), 100->█
    assert stx.sparkline([0, 50, 100]) == "▁▅█"
    assert len(stx.sparkline([1, 2, 3, 4, 5])) == 5


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
