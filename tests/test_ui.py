"""Drive the real TUI through the core flow: one episode → approve + rest of season → one batch approval."""
import os
from pathlib import Path

import make_library

from recast.config import Config, EncoderCap, Root
from recast.ui.app import RecastApp


async def wait(pilot, cond, secs=120):
    for _ in range(int(secs / 0.25)):
        await pilot.pause(0.25)
        if cond():
            return
    raise AssertionError("timed out")


async def test_episode_then_rest_of_season(home, tmp_path):
    lib = tmp_path / "lib"
    season = lib / "TV" / "Show (2020)" / "Season 01"
    for e in (1, 2, 3):
        make_library.episode(season / f"Show - S01E0{e} 1080p AV1.mkv",
                             ["-c:v", "libsvtav1", "-preset", "12", "-crf", "24"], secs=4)
    Config(roots=[Root("Lib", str(lib), remote=True)], scratch=str(tmp_path / "scratch"), ffmpeg="ffmpeg",
           ffprobe="ffprobe", ffmpeg_version="test", machine={"host": "test"}, desktop_notify=False,
           encoders={"libx265": EncoderCap("ok", 60), "libx264": EncoderCap("ok", 150),
                     "libsvtav1": EncoderCap("ok", 80)}).save()
    app = RecastApp()
    async with app.run_test(size=(200, 60)) as pilot:
        tree = app.query_one("#lib-tree")

        async def expand(label):
            await wait(pilot, lambda: any(n.data and os.path.basename(n.data.path) == label
                                          for n in tree._tree_nodes.values()), 10)
            n = next(n for n in tree._tree_nodes.values() if n.data and os.path.basename(n.data.path) == label)
            n.expand()
            return n
        await expand("TV")
        await expand("Show (2020)")
        s1 = await expand("Season 01")
        await wait(pilot, lambda: len(s1.children) == 3, 10)
        tree.move_cursor(s1.children[0])
        await pilot.press("e")
        await wait(pilot, lambda: type(app.screen).__name__ == "EncodeDialog", 15)
        app.screen.query_one("#f-preset").value = "Anime → HEVC · quality"
        await pilot.pause(0.3)
        app.screen.query_one("#f-speed").value = "ultrafast"
        await pilot.pause(0.3)
        await pilot.click("#go-preview")
        await wait(pilot, lambda: type(app.screen).__name__ == "ApprovalPrompt")
        assert [str(b.label) for b in app.screen.query("#appr-rest-btns Button")] == \
            ["✓ Approve + rest of Season 01 (2)  a"]
        await pilot.press("a")
        await wait(pilot, lambda: type(app.screen).__name__ == "EncodeDialog", 30)
        dlg = app.screen
        assert dlg.query_one("#f-preset").value == "Anime → HEVC · quality"
        assert dlg.query_one("#f-speed").value == "ultrafast"          # carried over, not reset
        assert [Path(f.path).name for f in dlg.files] == ["Show - S01E02 1080p AV1.mkv", "Show - S01E03 1080p AV1.mkv"]
        await pilot.click("#go-all")
        await wait(pilot, lambda: app.engine.batches and all(
            j.stage == "awaiting" for b in app.engine.batches.values() for j in app.engine.batch_jobs(b)))
        assert app.engine.jobs[1].stage == "replaced"
        assert len(app.engine.inbox()) == 1                              # one decision for the batch
        app.engine.approve(app.engine.inbox()[0])
        await wait(pilot, lambda: sorted(p.name for p in season.glob("*.mkv")) ==
                   [f"Show - S01E0{e} 1080p HEVC.mkv" for e in (1, 2, 3)])
        await wait(pilot, lambda: all("HEVC" in str(c.label) for c in s1.children), 10)   # tree refreshed
        assert {h["preset"] for h in app.engine.history} == {"Anime → HEVC · quality*"}


async def test_find_palette_searches_everything(home, tmp_path):
    lib = tmp_path / "lib"
    for show, n in (("Alpha Show (2020)", 2), ("Beta Show (2021)", 2)):
        for e in range(1, n + 1):
            make_library.episode(lib / show / "Season 1" / f"{show[:10]} - S01E0{e}.mkv",
                                 ["-c:v", "libx264", "-preset", "ultrafast"], size="320x240", secs=1, subs=False)
    Config(roots=[Root("Lib", str(lib), remote=False)], scratch=str(tmp_path / "scratch"), ffmpeg="ffmpeg",
           ffprobe="ffprobe", ffmpeg_version="t", machine={"host": "t"}, desktop_notify=False,
           encoders={"libx265": EncoderCap("ok", 60)}).save()
    app = RecastApp()
    async with app.run_test(size=(140, 40)) as pilot:
        tree = app.query_one("#lib-tree")
        await wait(pilot, lambda: tree.root.children and tree.root.children[0].data.loaded, 10)
        tree.move_cursor(tree.root.children[0])                 # highlight the library → overview scan
        await wait(pilot, lambda: app._scan is None and app.overview, 30)
        assert [s.name for s in app.overview[str(lib)]] and app.query_one("#wins").row_count == 2
        tree.root.children[0].collapse()
        await pilot.press("slash")
        await wait(pilot, lambda: type(app.screen).__name__ == "FindScreen", 5)
        await pilot.press(*"beta e02")
        await pilot.pause(0.3)
        assert [Path(h).name for h in app.screen.hits] == ["Beta Show  - S01E02.mkv"]
        await pilot.press("enter")
        await wait(pilot, lambda: tree.cursor_node and tree.cursor_node.data.path.endswith("S01E02.mkv")
                   and "Beta" in tree.cursor_node.data.path, 10)
