import base64

import pytest

from hwbp.models import ElectricalConnection, MechanicalConnection, Node, ProjectContent
from hwbp.services import (
    COLLABORATOR_LIMIT,
    Accounts,
    ChatLog,
    Collaboration,
    Community,
    InputImages,
    Notes,
    ServiceError,
    Turn,
    diff_plan,
)


def test_accounts_profile_and_credits():
    a = Accounts(default_plan_for={"vip": "pro"})
    assert a.get("vip").plan == "pro" and a.get("bob").plan == "free"
    assert a.set_username("bob", "Bob_1").username == "bob_1"
    with pytest.raises(ServiceError) as e:
        a.set_username("eve", "bob_1")
    assert e.value.code == "username_taken"
    with pytest.raises(ServiceError):
        a.set_username("eve", "x!")
    assert a.auto_username("eve").username and a.auto_username("eve").username == a.get("eve").username
    assert a.set_bio("eve", "hi").bio == "hi"
    with pytest.raises(ServiceError):
        a.set_bio("eve", "x" * 300)
    assert a.set_color("eve", "#AABBCC").profile_color == "#aabbcc"
    with pytest.raises(ServiceError):
        a.set_color("eve", "red")
    assert a.complete_onboarding("eve", {"role": "maker"}).onboarded
    assert a.set_email_prefs("eve", {"marketing": True, "bogus": 1}) == {
        "marketing": True,
        "productUpdates": True,
        "projectActivity": True,
    }
    assert a.credits("bob")["weekly_credits_remaining"] == 10
    for _ in range(10):
        a.consume_credit("bob")
    with pytest.raises(ServiceError) as e:
        a.consume_credit("bob")
    assert e.value.status == 402
    assert a.credits("bob", now=10**10)["weekly_credits_remaining"] == 10  # new week resets
    assert a.consume_credit("vip")["unlimited"]
    assert a.set_plan("bob", "ultra").plan == "ultra"
    with pytest.raises(ServiceError):
        a.set_plan("bob", "platinum")
    assert a.add_lead({"email": "a@b.co", "message": "hi"})["id"]
    with pytest.raises(ServiceError):
        a.add_lead({"email": "nope"})


def test_collaboration_roles_invites_links():
    c = Collaboration()
    c.set_owner("p", "own")
    with pytest.raises(ServiceError):
        c.require("p", "stranger", "viewer")
    inv = c.invite("p", "own", "Friend@Example.com", "editor", now=0)
    with pytest.raises(ServiceError) as e:
        c.invite("p", "own", "friend@example.com", "viewer", now=10)
    assert e.value.code == "invitation_cooldown"
    with pytest.raises(ServiceError):
        c.invite("p", "stranger", "x@y.z", "viewer")
    with pytest.raises(ServiceError) as e:
        c.accept_invite("friend", inv.token, now=10**12)
    assert e.value.code == "invitation_expired"
    assert c.accept_invite("friend", inv.token, now=1) == {"projectId": "p", "role": "editor"}
    with pytest.raises(ServiceError) as e:
        c.accept_invite("friend2", inv.token)  # single use
    assert e.value.code == "invitation_invalid"
    assert c.require("p", "friend", "editor") == "editor"
    c.change_role("p", "own", "friend", "viewer")
    with pytest.raises(ServiceError) as e:
        c.change_role("p", "own", "own", "viewer")
    assert e.value.code == "cannot_change_owner"
    with pytest.raises(ServiceError):
        c.change_role("p", "own", "ghost", "viewer")
    link = c.create_link("p", "own", "viewer", expires_in_h=1)
    assert c.redeem_link("v1", link.token) == {"projectId": "p", "role": "viewer"}
    assert c.redeem_link("friend", link.token)["role"] == "viewer"  # existing member keeps role
    with pytest.raises(ServiceError) as e:
        c.redeem_link("v2", link.token, now=10**12)
    assert e.value.code == "share_link_expired"
    c.revoke_link("p", "own", link.id)
    with pytest.raises(ServiceError) as e:
        c.redeem_link("v3", link.token)
    assert e.value.code == "share_link_invalid"
    big = c.create_link("p", "own", "viewer")
    for i in range(COLLABORATOR_LIMIT):
        try:
            c.redeem_link(f"u{i}", big.token)
        except ServiceError as err:
            assert err.code == "collaborator_limit_reached"
            break
    else:
        pytest.fail("limit not enforced")
    c.remove_member("p", "v1", "v1")  # leave
    with pytest.raises(ServiceError):
        c.remove_member("p", "own", "own")
    assert ("p", "owner") in c.projects_of("own")
    c.revoke_invite("p", "own", inv.id)
    with pytest.raises(ServiceError):
        c.revoke_invite("p", "own", "zzz")
    with pytest.raises(ServiceError):
        c.create_link("p", "own", "owner")


def test_community_feed_stars_trending_suggest():
    cm = Community()
    a = cm.publish("p1", "u", ProjectContent(project_name="Radar Scanner", project_description="tactical radar"), 1)
    b = cm.publish("p2", "u", ProjectContent(project_name="Plant Monitor"), 1)
    b["publishedAt"] -= 3600 * 48
    assert (
        cm.publish("p1", "u", ProjectContent(project_name="Radar Scanner v2"), 2)["token"] == a["token"]
    )  # republish keeps token
    assert cm.star(b["token"], "x") == {"starred": True, "starCount": 1}
    assert cm.star(b["token"], "x") == {"starred": False, "starCount": 0}
    cm.star(b["token"], "y")
    assert [p["projectName"] for p in cm.list(sort="stars")["projects"]][0] == "Plant Monitor"
    assert cm.list(sort="new")["projects"][0]["projectName"] == "Radar Scanner v2"
    assert cm.list(q="radar")["total"] == 1 and cm.list(limit=1)["hasMore"]
    with pytest.raises(ServiceError):
        cm.list(sort="random")
    assert cm.suggest("pla") == ["Plant Monitor"] and cm.suggest("an")
    assert [p["token"] for p in cm.starred("y")] == [b["token"]]
    assert cm.stats()["publishedProjects"] == 2
    assert cm.unpublish("p2") and not cm.unpublish("p2")
    with pytest.raises(ServiceError):
        cm.get(b["token"])
    assert Community.trending_score({"starCount": 0, "publishedAt": 0}, now=3600) < Community.trending_score(
        {"starCount": 5, "publishedAt": 0}, now=3600
    )


def test_notes_and_images():
    n = Notes()
    assert n.get("p")["version"] == 0
    assert n.put("p", "hello", 0, "u")["version"] == 1
    with pytest.raises(ServiceError) as e:
        n.put("p", "stale", 0, "u")
    assert e.value.code == "readme_conflict"
    im = InputImages()
    png = base64.b64encode(b"\x89PNG fake").decode()
    meta = im.add("data:image/png;base64," + png, draft_key="draft-1")
    assert meta["mimeType"] == "image/png" and len(meta["hash"]) == 64 and "bytes" not in meta
    assert im.get(meta["id"])["bytes"].startswith(b"\x89PNG")
    with pytest.raises(ServiceError):
        im.add("")
    with pytest.raises(ServiceError):
        im.add(png, mime="application/pdf")
    with pytest.raises(ServiceError) as e:
        im.get("zzz")
    assert e.value.code == "input_image_expired"


def test_diff_plan_and_chat_log():
    old = ProjectContent(
        nodes=[
            Node(id="a", name="A", type="mcu", catalog_id="x", pins=["P1"]),
            Node(id="b", name="B", type="sensor", catalog_id="y"),
        ],
        electrical_connections=[
            ElectricalConnection(type="data", source="b", target="a", source_pin="O", target_pin="P1")
        ],
        mechanical_connections=[MechanicalConnection(source="b", target="a")],
    )
    new = ProjectContent(
        nodes=[
            Node(id="a", name="A", type="mcu", catalog_id="x", pins=["P1", "P2"]),
            Node(id="b", name="B2", type="sensor", catalog_id="z"),
            Node(id="c", name="C", type="display"),
        ],  # fmt: skip
        electrical_connections=[
            ElectricalConnection(type="data", source="a", target="c", source_pin="P2", target_pin="IN")
        ],
    )
    plan = diff_plan(old, new)
    assert sorted(p["id"] for p in plan["addParts"]) == ["b", "c"] and plan["removeNodeIds"] == ["b"]
    assert len(plan["addElectricalConnections"]) == 1 and len(plan["removeElectricalConnections"]) == 1
    assert plan["removeMechanicalConnections"] == [{"source": "b", "target": "a"}]
    assert plan["newPinsUsedOnExistingNodes"] == [{"nodeId": "a", "pins": ["P2"]}]
    assert "add" in plan["plan"] and diff_plan(old, old)["plan"] == "rewire / re-layout only"
    log = ChatLog()
    t = log.add(
        "p", Turn(id="t1", prompt="hi", mode="ask", actor="u", status="completed", base_version=1, created_at=0)
    )
    assert log.find("p", "t1") is t and log.snapshot("p", 3)["turns"][0]["id"] == "t1"
    with pytest.raises(ServiceError):
        log.find("p", "zz")
    assert log.clear("p")["historyEpoch"] == 1 and log.snapshot("p", 3)["turns"] == []
