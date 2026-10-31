"""testflight_notes.py: the TestFlight "What to test" notes composed from may-app's pull-request titles."""

import unittest

import testflight_notes as notes

SHA, OLD, OLDER = "c" * 40, "a" * 40, "b" * 40


def run(name, conclusion="success", status="completed"):
    return {"display_title": name, "status": status, "conclusion": conclusion}


class PreviousBuildTests(unittest.TestCase):
    def test_the_newest_successful_upload_of_the_same_app_before_this_build(self):
        runs = [run(f"May Playground · iOS · build 12 at {OLDER}"),
                run(f"May Playground · iOS · build 14 at {OLD}"),
                run(f"May Playground · iOS · build 15 at {OLDER} (dry run)"),
                run(f"May Playground · iOS · build 16 at {OLDER}", conclusion="failure"),
                run(f"May · iOS · build 20 at {OLDER}"),
                run(f"May Playground · Android · build 30 at {OLDER}"),
                run(f"May Playground · iOS · build 13 at {OLDER} (sign only)"),
                run(f"May Playground · iOS · build 18 at {OLDER}"),
                run("May Playground · iOS · build 11")]
        self.assertEqual(notes.previous_build(runs, "playground", 17, SHA), OLD)
        self.assertEqual(notes.previous_build(runs, "production", 21, SHA), OLDER)
        self.assertIsNone(notes.previous_build(runs, "production", 20, SHA), "not this build itself")
        self.assertIsNone(notes.previous_build([run(f"May Playground · iOS · build 14 at {SHA}")], "playground", 17,
                                               SHA), "the same commit is not a previous build")
        self.assertIsNone(notes.previous_build([], "playground", 17, SHA))


class ComposeTests(unittest.TestCase):
    def test_titles_are_grouped_by_area_as_try_lines_without_their_numbers(self):
        text = notes.compose(["Brief: needs_you_count, everything waiting on the person (173) (#1503)",
                              "Agenda: the soft edge under the pinned week strip (#1492)",
                              "Brief: a second change (#1510)",
                              "A title with no area at all (#1499)",
                              "Bad\x07control: kept\nsafe (#1)"])
        self.assertEqual(text.splitlines(), [
            "Try: Brief: needs_you_count, everything waiting on the person (173)",
            "Try: Brief: a second change",
            "Try: Agenda: the soft edge under the pinned week strip",
            "Try: Other: A title with no area at all",
            "Try: Bad control: kept safe"])

    def test_nothing_new_says_so(self):
        self.assertEqual(notes.compose([]), notes.NOTHING_NEW)
        self.assertEqual(notes.compose(["  ", "(#3)"]), notes.NOTHING_NEW)

    def test_long_notes_keep_whole_lines_under_the_limit_and_count_the_rest(self):
        titles = [f"Area {i % 3}: change number {i} " + "x" * 80 for i in range(100)]
        text = notes.compose(titles)
        self.assertLessEqual(len(text), notes.NOTES_LIMIT)
        lines = text.splitlines()
        self.assertRegex(lines[-1], r"^…and \d+ more$")
        self.assertEqual(len(lines) - 1 + int(lines[-1].split()[1]), 100, "every title is shown or counted")
        self.assertTrue(all(line.startswith("Try: Area ") for line in lines[:-1]))


if __name__ == "__main__":
    unittest.main()
