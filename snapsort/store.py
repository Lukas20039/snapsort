"""Folder listing, tag store, filters, rounds and moves. No Qt."""
import copy
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".avif"}
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm"}
STORE_NAME = ".snapsort.json"
VERDICTS = ("keep", "reject")


def natural_key(name: str):
    return tuple(int(p) if p.isdigit() else p.casefold() for p in re.split(r"(\d+)", name))


def is_video(name: str) -> bool:
    return Path(name).suffix.lower() in VIDEO_EXT


def list_media(folder) -> list[str]:
    exts = IMAGE_EXT | VIDEO_EXT
    with os.scandir(folder) as it:
        names = [e.name for e in it
                 if not e.name.startswith(".") and e.is_file() and Path(e.name).suffix.lower() in exts]
    return sorted(names, key=natural_key)


def atomic_write(path, data: bytes, fsync=True) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        if path.exists():
            shutil.copymode(path, tmp)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class TagStore:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.path = self.folder / STORE_NAME
        self.round = 0
        self.files: dict[str, dict] = {}
        self.warning = None
        try:
            raw = json.loads(self.path.read_text())
            self.round = int(raw.get("round", 0))
            self.files = {}
            for k, v in raw.get("files", {}).items():
                if not isinstance(v.get("tags", []), list):
                    raise TypeError("tags must be a list")
                self.files[k] = {"tags": [str(t) for t in v.get("tags", [])], "round": int(v.get("round", 0))}
        except FileNotFoundError:
            pass
        except OSError as e:
            self.warning = f"Cannot read {STORE_NAME}: {e.strerror}"
        except (ValueError, AttributeError, TypeError):
            self.round, self.files = 0, {}
            bad = self.path.with_name(f"{STORE_NAME}.bad-{int(time.time())}")
            try:
                os.replace(self.path, bad)
                self.warning = f"Corrupt {STORE_NAME} moved to {bad.name}"
            except OSError as e:
                self.warning = f"Corrupt {STORE_NAME}, cannot move it aside: {e.strerror}"

    def tags(self, name) -> set[str]:
        return set(self.files.get(name, {}).get("tags", ()))

    def survived(self, name) -> int:
        return self.files.get(name, {}).get("round", 0)

    def all_tags(self) -> set[str]:
        return {t for v in self.files.values() for t in v["tags"]}

    def _entry(self, name):
        return self.files.setdefault(name, {"tags": [], "round": 0})

    def toggle(self, name, tag) -> None:
        tags = self._entry(name)["tags"]
        tags.remove(tag) if tag in tags else tags.append(tag)
        self.save()

    def set_verdict(self, name, tag) -> None:
        tags = self._entry(name)["tags"]
        had = tag in tags
        tags[:] = [t for t in tags if t not in VERDICTS]
        if not had:
            tags.append(tag)
        self.save()

    def save(self) -> bool:
        """Write to disk; on failure keep the data in memory and set self.warning."""
        # ponytail: sync save per mutation, debounce with a QTimer if folders reach ~50k files
        files = {k: {"tags": sorted(v["tags"]), **({"round": v["round"]} if v["round"] else {})}
                 for k, v in self.files.items() if v["tags"] or v["round"]}
        if not files and not self.round and not self.path.exists():
            return True
        data = {"version": 1, "round": self.round, "files": files}
        try:
            atomic_write(self.path, json.dumps(data, indent=1, sort_keys=True).encode(), fsync=False)
        except OSError as e:
            self.warning = f"Tags NOT saved ({self.folder}): {e.strerror}"
            return False
        self.warning = None
        return True


@dataclass
class Filter:
    include: set = field(default_factory=set)
    exclude: set = field(default_factory=set)
    mode_all: bool = False
    untagged: bool = False
    min_round: int = 0

    def matches(self, tags: set, survived: int) -> bool:
        if survived < self.min_round:
            return False
        if self.untagged and tags:
            return False
        if self.include and not (self.include <= tags if self.mode_all else self.include & tags):
            return False
        return not (self.exclude & tags)

    def describe(self) -> str:
        parts = sorted(self.include)
        s = (" ∧ " if self.mode_all else " ∨ ").join(parts)
        s += "".join(f" ∧ ¬{t}" for t in sorted(self.exclude))
        if self.untagged:
            s += " untagged"
        if self.min_round:
            s += f" round≥{self.min_round}"
        return s.strip(" ∧") or "none"


def round_counts(store, names):
    keep = sum("keep" in store.tags(n) for n in names)
    reject = sum("reject" in store.tags(n) for n in names)
    return keep, reject, len(names) - keep - reject


def next_round(store, names, undecided_keep: bool) -> dict:
    """Survivors (kept, plus undecided if undecided_keep) advance; returns an undo snapshot."""
    snap = {"round": store.round,
            "files": {n: copy.deepcopy(store.files[n]) if n in store.files else None for n in names}}
    new = store.round + 1
    for n in names:
        tags = store.tags(n)
        if "keep" in tags or ("reject" not in tags and undecided_keep):
            store._entry(n)["round"] = new
        if n in store.files:
            store.files[n]["tags"] = [t for t in store.files[n]["tags"] if t not in VERDICTS]
    store.round = new
    store.save()
    return snap


def restore_snapshot(store, snap) -> None:
    """Undo next_round: restore round and verdicts of the round's files, keep later tags."""
    store.round = snap["round"]
    for n, old in snap["files"].items():
        cur = store.files.get(n)
        if cur is None and old is None:
            continue
        tags = [t for t in (cur or {"tags": []})["tags"] if t not in VERDICTS]
        tags += [t for t in (old or {"tags": []})["tags"] if t in VERDICTS]
        store.files[n] = {"tags": tags, "round": (old or {"round": 0})["round"]}
    store.save()


def _free_name(name, dst: Path, taken: set) -> str:
    p = Path(name)
    cand, i = name, 0
    while cand in taken or (dst / cand).exists():
        i += 1
        cand = f"{p.stem} ({i}){p.suffix}"
    return cand


def plan_move(names, dst_dir) -> list[tuple[str, str]]:
    dst, taken, plan = Path(dst_dir), set(), []
    for n in names:
        new = _free_name(n, dst, taken)
        taken.add(new)
        plan.append((n, new))
    return plan


@dataclass
class MoveResult:
    src: Path
    dst: Path
    moved: list  # [(old, new)]
    created: bool
    error: str | None = None


def execute_move(store, plan, dst_dir) -> MoveResult:
    dst = Path(dst_dir)
    created = not dst.exists()
    dst.mkdir(exist_ok=True)
    res = MoveResult(store.folder, dst, [], created)
    taken = {new for _, new in plan}
    for old, new in plan:
        if (dst / new).exists():
            # ponytail: exists-check+rename has a TOCTOU window; fine single-user
            new = _free_name(old, dst, taken)
            taken.add(new)
        try:
            os.rename(store.folder / old, dst / new)
        except OSError as e:
            res.error = f"{old}: {e.strerror}"
            break
        res.moved.append((old, new))
    target = TagStore(dst)
    for old, new in res.moved:
        if old in store.files:
            target.files[new] = store.files.pop(old)
    target.save()
    store.save()
    return res


def undo_move(store, res: MoveResult) -> list[str]:
    """Move files back; returns names that could not be restored."""
    target = TagStore(res.dst)
    failed = []
    for old, new in res.moved:
        if (res.src / old).exists():
            failed.append(old)
            continue
        try:
            os.rename(res.dst / new, res.src / old)
        except OSError:
            failed.append(old)
            continue
        if new in target.files:
            store.files[old] = target.files.pop(new)
    target.save()
    store.save()
    if res.created:
        if target.path.exists() and not target.files:
            target.path.unlink()
        try:
            res.dst.rmdir()
        except OSError:
            pass
    return failed
