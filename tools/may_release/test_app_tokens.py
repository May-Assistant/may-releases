"""The May CI GitHub App's tokens (static_check.unminted): each job that runs pull-request code mints its narrowed token
right after its first checkout, before any pull-request code, and hands it on in place of the PAT, which stays the
fallback while MAY_CI_APP_ID is unset. The private key is in the mint step alone. Nothing here reaches the network."""

from pathlib import Path
import unittest

import static_check

WORKFLOWS = static_check.ROOT / ".github/workflows"
CHECKERS = {
    "native-compile-pr.yml": static_check.compile_pr_workflow_problems,
    "android-unit-pr.yml": static_check.android_unit_workflow_problems,
    "ios-archive-rehearsal.yml": static_check.ios_rehearsal_workflow_problems,
    "android-ci-schedule.yml": static_check.android_ci_schedule_workflow_problems,
    "backend-unit-pr.yml": static_check.backend_unit_workflow_problems,
    "backend-ci-schedule.yml": static_check.backend_ci_schedule_workflow_problems,
    "ios-core-pr.yml": static_check.ios_core_workflow_problems,
    "ios-core-schedule.yml": static_check.ios_core_schedule_workflow_problems,
    "ios-renders.yml": static_check.renders_workflow_problems,
    "ds-swift-compile.yml": static_check.ds_compile_workflow_problems,
    "may-app-review.yml": static_check.review_workflow_problems,
    "weekly-release.yml": static_check.weekly_release_workflow_problems,
    "may-ds-review.yml": static_check.ds_review_workflow_problems,
    "device-tests.yml": static_check.device_tests_workflow_problems,
    "testflight-feedback.yml": static_check.testflight_feedback_workflow_problems,
    "ds-tests-pr.yml": static_check.ds_tests_workflow_problems,
    "ds-tests-schedule.yml": static_check.ds_tests_schedule_workflow_problems,
}
SCHEDULE = "android-ci-schedule.yml"
REVIEW = "may-app-review.yml"
DEVICE = "device-tests.yml"
READ_CONSUMER = "MAY_APP_PR_READ_TOKEN: ${{ steps.ci-read.outputs.token || secrets.MAY_APP_PR_READ_TOKEN }}\n"


def workflow(name):
    return (WORKFLOWS / name).read_text()


class CommittedMintTests(unittest.TestCase):
    def test_every_committed_workflow_mints_each_token_and_passes_its_own_checker(self):
        self.assertEqual(set(CHECKERS), set(static_check.WORKFLOW_MINTS))
        for name, check in CHECKERS.items():
            with self.subTest(workflow=name):
                text = workflow(name)
                plain, found = static_check.unminted(name, text)
                self.assertEqual(found, [])
                self.assertEqual(check(text), [])
                for mints in static_check.WORKFLOW_MINTS[name].values():
                    for step_id in mints:
                        self.assertIn(static_check.mint_step(step_id), text)
                for trace in static_check.APP_TOKEN_TRACES:
                    self.assertNotIn(trace, plain)

    def test_each_mint_reaches_one_repository_with_the_pats_permissions_alone(self):
        step = static_check.mint_step("ci-review")
        self.assertIn("          owner: May-Assistant\n          repositories: may-app\n", step)
        self.assertTrue(step.endswith("          permission-contents: read\n          permission-pull-requests: write\n"))
        self.assertIn("        if: vars.MAY_CI_APP_ID != ''\n", step)
        self.assertNotIn("continue-on-error", step)
        self.assertRegex(static_check.CREATE_APP_TOKEN, r"@[0-9a-f]{40}$")
        self.assertIn("permission-pull-requests: read\n", static_check.mint_step("ci-ds-read"))
        self.assertIn("repositories: may-design-system\n", static_check.mint_step("ci-ds-read"))


class MintMutationTests(unittest.TestCase):
    def refused(self, name, old, new, count=1):
        text = workflow(name)
        self.assertIn(old, text, "mutation anchor not found: " + old[:60])
        return CHECKERS[name](text.replace(old, new, count))

    def assert_refused(self, name, old, new, count=1):
        found = self.refused(name, old, new, count)
        self.assertTrue(any(name + ": " in problem for problem in found), found)

    def test_a_mint_without_its_narrowing_is_refused(self):
        for old in ("          repositories: may-app\n", "          permission-pull-requests: read\n",
                    "          owner: May-Assistant\n"):
            with self.subTest(removed=old.strip()):
                self.assert_refused(SCHEDULE, old, "")

    def test_a_wider_mint_is_refused(self):
        for old, new in (("repositories: may-app\n", "repositories: may-app,may-releases\n"),
                         ("permission-pull-requests: read\n", "permission-pull-requests: write\n"),
                         ("permission-pull-requests: read\n",
                          "permission-pull-requests: read\n          permission-contents: write\n")):
            with self.subTest(new=new.strip()):
                self.assert_refused(SCHEDULE, old, new)

    def test_an_unpinned_or_other_action_is_refused(self):
        for new in ("actions/create-github-app-token@v3 # v3.2.0",
                    "actions/create-github-app-token@" + "0" * 40 + " # v3.2.0"):
            with self.subTest(new=new):
                self.assert_refused(SCHEDULE, static_check.CREATE_APP_TOKEN + " # v3.2.0", new)

    def test_a_mint_that_does_not_fail_the_job_or_always_runs_is_refused(self):
        self.assert_refused(SCHEDULE, "        if: vars.MAY_CI_APP_ID != ''\n",
                            "        if: vars.MAY_CI_APP_ID != ''\n        continue-on-error: true\n")
        self.assert_refused(SCHEDULE, "        if: vars.MAY_CI_APP_ID != ''\n", "")

    def test_a_mint_after_another_step_is_refused(self):
        text = workflow(SCHEDULE)
        mint = static_check.mint_step("ci-read")
        runs = text[text.index("      - name: List the unit runs"):text.index("      - name: Pick the oldest")]
        moved = text.replace(mint, "").replace(runs, runs + mint)
        self.assertNotEqual(moved, text)
        self.assertTrue(static_check.android_ci_schedule_workflow_problems(moved))

    def test_the_private_key_anywhere_but_the_mint_is_refused(self):
        key = "          MAY_CI_APP_PRIVATE_KEY: ${{ secrets.MAY_CI_APP_PRIVATE_KEY }}\n"
        self.assert_refused(SCHEDULE, "        env:\n          MAY_APP_PR_READ_TOKEN",
                            "        env:\n" + key + "          MAY_APP_PR_READ_TOKEN")
        self.assert_refused(SCHEDULE, "    timeout-minutes: 10\n",
                            "    timeout-minutes: 10\n    env:\n" + key[4:])

    def test_a_token_that_is_not_handed_on_in_place_of_the_pat_is_refused(self):
        self.assert_refused(SCHEDULE, READ_CONSUMER, "MAY_APP_PR_READ_TOKEN: ${{ secrets.MAY_APP_PR_READ_TOKEN }}\n")
        self.assert_refused(SCHEDULE, READ_CONSUMER, "MAY_APP_PR_READ_TOKEN: ${{ steps.ci-read.outputs.token }}\n")

    def test_a_token_handed_to_a_step_that_held_no_pat_is_refused(self):
        self.assert_refused(SCHEDULE, "          GH_TOKEN: ${{ github.token }}\n        run: python3 -I -S -B "
                            "tools/may_release/android_ci_schedule.py dispatch",
                            "          GH_TOKEN: ${{ steps.ci-read.outputs.token || github.token }}\n        run: "
                            "python3 -I -S -B tools/may_release/android_ci_schedule.py dispatch")

    def test_a_missing_mint_or_one_in_another_job_is_refused(self):
        self.assert_refused(SCHEDULE, static_check.mint_step("ci-read"), "")
        text = workflow(SCHEDULE)
        admission_checkout = text.index(static_check.FIRST_CHECKOUT)
        at = admission_checkout + len(static_check.FIRST_CHECKOUT)
        extra = text[:at] + static_check.mint_step("ci-read").replace("id: ci-read", "id: ci-read-2") + text[at:]
        self.assertTrue(static_check.android_ci_schedule_workflow_problems(extra))

    def test_the_renders_release_is_minted_by_the_may_renders_app_alone(self):
        step = static_check.mint_step("renders-release")
        self.assertIn("        if: vars.MAY_RENDERS_APP_ID != ''\n", step)
        self.assertIn("          private-key: ${{ secrets.MAY_RENDERS_APP_PRIVATE_KEY }}\n", step)
        self.assertTrue(step.endswith("          repositories: may-renders\n          permission-contents: write\n"))
        self.assertNotIn("MAY_CI_APP", step)
        for old, new in (("${{ vars.MAY_RENDERS_APP_ID }}", "${{ vars.MAY_CI_APP_ID }}"),
                         ("${{ secrets.MAY_RENDERS_APP_PRIVATE_KEY }}", "${{ secrets.MAY_CI_APP_PRIVATE_KEY }}"),
                         ("repositories: may-renders\n", "repositories: may-app\n"),
                         ("permission-contents: write\n", "permission-contents: write\n          permission-pull-requests: write\n")):
            with self.subTest(new=new.strip()):
                self.assert_refused(DEVICE, old, new)
        release = "MAY_RENDERS_RELEASE_TOKEN: ${{ steps.renders-release.outputs.token || secrets.MAY_RENDERS_RELEASE_TOKEN }}\n"
        self.assert_refused(DEVICE, release, "MAY_RENDERS_RELEASE_TOKEN: ${{ steps.renders-release.outputs.token }}\n")

    def test_the_review_slug_goes_to_the_fetch_step_alone(self):
        for name, step_id, check in ((REVIEW, "ci-review", static_check.review_workflow_problems),
                                     ("may-ds-review.yml", "ci-ds-review", static_check.ds_review_workflow_problems)):
            with self.subTest(workflow=name):
                slug = static_check.REVIEW_SLUG.format(step_id)
                text = workflow(name)
                self.assertEqual(text.count(slug), 1)
                review_env = "          MAY_REVIEW_CLAUDE_TOKEN: ${{ secrets.MAY_REVIEW_CLAUDE_TOKEN }}\n"
                self.assert_refused(name, slug, "")
                self.assert_refused(name, review_env, review_env + slug)
                self.assertTrue(check(text.replace(slug, "").replace(review_env, review_env + slug)))


    def test_each_post_alone_is_told_its_comment_apps_slug(self):
        """Review of may-releases #137: each post counts only its own app's bot comments, so it is told the slug."""
        self.assertEqual(set(static_check.COMMENT_SLUG_POSTS),
                         {("native-compile-pr.yml", "post"), ("native-compile-pr.yml", "swift-checks"),
                          ("ios-core-pr.yml", "core"), ("native-compile-pr.yml", "core"), ("ios-renders.yml", "publish"),
                          ("ds-swift-compile.yml", "post")},
                         "every post that dedupes on its head's marker")
        for (name, job), (step_id, run) in static_check.COMMENT_SLUG_POSTS.items():
            with self.subTest(workflow=name, job=job):
                slug = static_check.comment_slug(step_id)
                anchor = slug + "        run: python3 -I -S -B tools/may_release/" + run
                text = workflow(name)
                self.assertEqual(text.count(anchor), 1)
                self.assert_refused(name, anchor, anchor.replace(slug, ""))
                self.assert_refused(name, anchor, slug + anchor)
                other = slug.replace("app-slug", "token")
                self.assert_refused(name, anchor, anchor.replace(slug, other))


if __name__ == "__main__":
    unittest.main()
