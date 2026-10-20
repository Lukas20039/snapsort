import json

from snapsort import store as st
from snapsort.store import Filter, TagStore


def test_natural_key():
    assert sorted(["IMG_10.jpg", "img_2.jpg", "IMG_1.jpg"], key=st.natural_key) == \
        ["IMG_1.jpg", "img_2.jpg", "IMG_10.jpg"]


def test_list_media(tmp_path):
    for n in ["a.JPG", "b.mp4", ".hidden.jpg", "c.txt", ".snapsort.json", "d.HEIC"]:
        (tmp_path / n).write_bytes(b"x")
    (tmp_path / "sub.jpg").mkdir()
    assert st.list_media(tmp_path) == ["a.JPG", "b.mp4", "d.HEIC"]


def test_tagstore_roundtrip(tmp_path):
    s = TagStore(tmp_path)
    s.toggle("a.jpg", "fav")
    s.toggle("b.jpg", "x")
    s.toggle("b.jpg", "x")  # empty again -> pruned
    t = TagStore(tmp_path)
    assert t.tags("a.jpg") == {"fav"} and "b.jpg" not in json.loads(t.path.read_text())["files"]
    assert [p.name for p in tmp_path.iterdir()] == [".snapsort.json"]


def test_tagstore_no_file_when_nothing_tagged(tmp_path):
    TagStore(tmp_path).save()
    assert not (tmp_path / ".snapsort.json").exists()


def test_tagstore_corrupt(tmp_path):
    (tmp_path / ".snapsort.json").write_text("{nope")
    s = TagStore(tmp_path)
    assert s.files == {} and s.warning
    assert any(p.name.startswith(".snapsort.json.bad-") for p in tmp_path.iterdir())


def test_verdict_exclusive(tmp_path):
    s = TagStore(tmp_path)
    s.set_verdict("a", "keep")
    s.set_verdict("a", "reject")
    assert s.tags("a") == {"reject"}
    s.set_verdict("a", "reject")
    assert s.tags("a") == set()


def test_filter():
    T = {"fav", "car"}
    assert Filter().matches(set(), 0)
    assert Filter(include={"fav"}).matches(T, 0)
    assert Filter(include={"fav", "x"}).matches(T, 0)
    assert not Filter(include={"fav", "x"}, mode_all=True).matches(T, 0)
    assert Filter(include={"fav", "car"}, mode_all=True).matches(T, 0)
    assert not Filter(exclude={"car"}).matches(T, 0)
    assert Filter(untagged=True).matches(set(), 0)
    assert not Filter(untagged=True).matches(T, 0)
    assert not Filter(min_round=1).matches(T, 0)
    assert Filter(min_round=1).matches(T, 2)


def test_next_round(tmp_path):
    s = TagStore(tmp_path)
    s.set_verdict("a", "keep")
    s.set_verdict("b", "reject")
    s.set_verdict("out", "keep")  # not in view
    snap = next_snap = st.next_round(s, ["a", "b", "c"], undecided_keep=True)
    assert s.round == 1
    assert s.survived("a") == 1 and s.survived("c") == 1 and s.survived("b") == 0
    assert s.tags("a") == set() and s.tags("out") == {"keep"}
    st.restore_snapshot(s, next_snap)
    assert s.round == 0 and s.tags("a") == {"keep"} and s.survived("c") == 0
    assert snap is next_snap
    st.next_round(s, ["a", "b", "c"], undecided_keep=False)
    assert s.survived("c") == 0


def test_plan_move(tmp_path):
    (tmp_path / "x.jpg").write_bytes(b"")
    plan = st.plan_move(["x.jpg", "y", "a.tar.gz"], tmp_path)
    assert plan == [("x.jpg", "x (1).jpg"), ("y", "y"), ("a.tar.gz", "a.tar.gz")]
    (tmp_path / "a.tar.gz").write_bytes(b"")
    assert st.plan_move(["a.tar.gz"], tmp_path) == [("a.tar.gz", "a.tar (1).gz")]


def test_move_and_undo(tmp_path):
    for n in ["x.jpg", "y.jpg"]:
        (tmp_path / n).write_bytes(n.encode())
    s = TagStore(tmp_path)
    s.toggle("x.jpg", "fav")
    dst = tmp_path / "best"
    dst.mkdir()
    (dst / "x.jpg").write_bytes(b"existing")
    res = st.execute_move(s, st.plan_move(["x.jpg", "y.jpg"], dst), dst)
    assert (dst / "x.jpg").read_bytes() == b"existing"
    assert (dst / "x (1).jpg").read_bytes() == b"x.jpg"
    assert TagStore(dst).tags("x (1).jpg") == {"fav"} and "x.jpg" not in TagStore(tmp_path).files
    assert st.undo_move(s, res) == []
    assert (tmp_path / "x.jpg").read_bytes() == b"x.jpg" and TagStore(tmp_path).tags("x.jpg") == {"fav"}
    assert (dst / "x.jpg").exists()  # pre-existing folder kept


def test_undo_removes_created_folder_and_reports_conflicts(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"a")
    (tmp_path / "b.jpg").write_bytes(b"b")
    s = TagStore(tmp_path)
    s.toggle("a.jpg", "fav")
    res = st.execute_move(s, st.plan_move(["a.jpg", "b.jpg"], tmp_path / "new"), tmp_path / "new")
    (tmp_path / "b.jpg").write_bytes(b"reused")
    assert st.undo_move(s, res) == ["b.jpg"]
    assert (tmp_path / "new" / "b.jpg").exists() and (tmp_path / "a.jpg").exists()
    (tmp_path / "b.jpg").unlink()
    res2 = st.execute_move(s, st.plan_move(["a.jpg"], tmp_path / "n2"), tmp_path / "n2")
    st.undo_move(s, res2)
    assert not (tmp_path / "n2").exists()


def test_undo_round_keeps_later_tags(tmp_path):
    s = TagStore(tmp_path)
    s.set_verdict("a", "keep")
    snap = st.next_round(s, ["a", "b"], undecided_keep=False)
    s.toggle("a", "car")          # tagged after the round
    s.set_verdict("a", "reject")  # verdict in the new round
    st.restore_snapshot(s, snap)
    assert s.tags("a") == {"car", "keep"} and s.survived("a") == 0 and s.round == 0


def test_save_failure_keeps_memory_and_warns(tmp_path, monkeypatch):
    s = TagStore(tmp_path)
    monkeypatch.setattr(st, "atomic_write", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied")))
    s.toggle("a", "fav")
    assert s.tags("a") == {"fav"} and "NOT saved" in s.warning


def test_non_list_tags_is_corrupt(tmp_path):
    (tmp_path / ".snapsort.json").write_text('{"files": {"a": {"tags": "fav"}}}')
    s = TagStore(tmp_path)
    assert s.files == {} and "Corrupt" in s.warning
