import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bump_versions as bv


def write(path, content):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


ANYVM_PY = (
    'VERSION = "x"\n'
    "DEFAULT_BUILDER_VERSIONS = {\n"
    '    "freebsd": "2.2.5",\n'
    '    "openbsd": "2.0.9",\n'
    "}\n"
)

WORKFLOW = (
    "name: Demo\n"
    "jobs:\n"
    "  test:\n"
    "    strategy:\n"
    "      matrix:\n"
    '        release: ["15.0", "15.1"]\n'
    '        arch: ["aarch64", ""]\n'
    '        sync: ["nfs", "scp"]\n'
    "  test-riscv:\n"
    "    strategy:\n"
    "      matrix:\n"
    '        release: ["15.1"]\n'
    '        arch: ["riscv64"]\n'
)


def rel(tag, release=None, arch="x86_64", desktop=False, build=True):
    return {"tag": tag, "release": release or tag, "arch": arch,
            "sync": "nfs,scp", "shutdown": "shutdown",
            "desktop": desktop, "build": build}


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(tmp.name)
        write("anyvm.py", ANYVM_PY)
        write(os.path.join(".github", "workflows", "demo.yml"), WORKFLOW)
        write(os.path.join(".github", "coverage.allow"),
              "demo r1-build demo.yml\n")


class TestDefaultVersions(Case):
    def test_line_rewrite(self):
        n = bv.rewrite_default_version("freebsd", "2.2.6")
        self.assertTrue(n)
        text = open("anyvm.py").read()
        self.assertIn('    "freebsd": "2.2.6",', text)
        self.assertIn('    "openbsd": "2.0.9",', text)

    def test_unknown_os_is_not_invented(self):
        self.assertFalse(bv.rewrite_default_version("plan10", "1.0.0"))
        self.assertNotIn("plan10", open("anyvm.py").read())


class TestMatrixEdit(Case):
    def test_release_added_when_all_job_arches_ship(self):
        index = [rel("15.0"), rel("15.0", arch="aarch64"),
                 rel("15.1"), rel("15.1", arch="aarch64"),
                 rel("15.1", arch="riscv64"),
                 rel("15.2"), rel("15.2", arch="aarch64"),
                 rel("15.2", arch="riscv64")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        text = open(".github/workflows/demo.yml").read()
        self.assertIn('release: ["15.0", "15.1", "15.2"]', text)
        self.assertIn('release: ["15.1", "15.2"]', text)

    def test_release_missing_on_one_arch_is_not_added(self):
        index = [rel("15.1"), rel("15.1", arch="aarch64"),
                 rel("15.1", arch="riscv64"),
                 rel("15.2"), rel("15.2", arch="aarch64")]  # no riscv64
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        text = open(".github/workflows/demo.yml").read()
        # job with aarch64+"" gets it; riscv64 job does not
        self.assertIn('release: ["15.0", "15.1", "15.2"]', text)
        self.assertIn('release: ["15.1"]\n', text)
        self.assertTrue(any("riscv" in n for n in notes))

    def test_empty_arch_means_x86_64(self):
        index = [rel("15.0"), rel("15.1"),
                 rel("15.2")]   # x86_64 only
        changed, notes = bv.extend_matrices("demo", index)
        text = open(".github/workflows/demo.yml").read()
        # first job needs aarch64 too -> not added anywhere
        self.assertNotIn("15.2", text)
        self.assertTrue(notes)

    def test_desktop_and_variants_never_enter_matrices(self):
        index = [rel("15.0"), rel("15.0", arch="aarch64"),
                 rel("15.1"), rel("15.1", arch="aarch64"),
                 rel("15.1", arch="riscv64"),
                 rel("15.2-xfce", desktop=True),
                 rel("15.2-build")]
        changed, notes = bv.extend_matrices("demo", index)
        text = open(".github/workflows/demo.yml").read()
        self.assertNotIn("xfce", text)
        self.assertNotIn("15.2-build", text)

    def test_comments_and_excludes_survive_byte_identically(self):
        wf = (".github/workflows/demo.yml")
        extra = ("        exclude:\n"
                 "          # hand-written knowledge, never touched\n"
                 '          - release: "15.0"\n'
                 "            sync: rsync\n")
        write(wf, WORKFLOW + extra)
        before = open(wf).read()
        index = [rel("15.0"), rel("15.0", arch="aarch64"),
                 rel("15.1"), rel("15.1", arch="aarch64"),
                 rel("15.1", arch="riscv64")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertFalse(changed)
        self.assertEqual(open(wf).read(), before)

    def test_ordering_follows_natural_key(self):
        write(".github/workflows/demo.yml",
              "jobs:\n  t:\n    strategy:\n      matrix:\n"
              '        release: ["9.4", "10.0"]\n'
              '        arch: [""]\n')
        index = [rel("9.4"), rel("10.0"), rel("10.1")]
        bv.extend_matrices("demo", index)
        self.assertIn('release: ["9.4", "10.0", "10.1"]',
                      open(".github/workflows/demo.yml").read())

    def test_sentinel_default_release_list_is_never_touched(self):
        # freebsd's cross-host powerpc64 job: release: [""] means "the
        # default release only"
        wf = ("jobs:\n  t:\n    strategy:\n      matrix:\n"
              '        release: ["15.1"]\n'
              '        arch: [""]\n'
              "  hosts:\n    strategy:\n      matrix:\n"
              '        release: [""]\n'
              '        arch: ["powerpc64"]\n')
        write(".github/workflows/demo.yml", wf)
        index = [rel("15.1"), rel("15.1", arch="powerpc64"),
                 rel("15.2"), rel("15.2", arch="powerpc64")]
        bv.extend_matrices("demo", index)
        text = open(".github/workflows/demo.yml").read()
        self.assertIn('release: [""]\n', text)
        self.assertIn('release: ["15.1", "15.2"]', text)

    def test_empty_release_list_is_never_touched(self):
        wf = ("jobs:\n  t:\n    strategy:\n      matrix:\n"
              "        release: []\n"
              '        arch: [""]\n')
        write(".github/workflows/demo.yml", wf)
        changed, notes = bv.extend_matrices("demo", [rel("15.2")])
        self.assertFalse(changed)
        self.assertIn("release: []\n",
                      open(".github/workflows/demo.yml").read())

    def test_frozen_legacy_job_is_never_extended(self):
        # openbsd's testold: deliberately pinned old releases; only jobs
        # already tracking the file-wide newest release keep tracking
        wf = ("jobs:\n  test:\n    strategy:\n      matrix:\n"
              '        release: ["7.8", "7.9"]\n'
              '        arch: [""]\n'
              "  testold:\n    strategy:\n      matrix:\n"
              '        release: ["7.3", "7.4", "7.5", "7.6"]\n'
              '        arch: [""]\n')
        write(".github/workflows/demo.yml", wf)
        index = [rel("7.8"), rel("7.9"), rel("8.0")]
        bv.extend_matrices("demo", index)
        text = open(".github/workflows/demo.yml").read()
        self.assertIn('release: ["7.8", "7.9", "8.0"]', text)
        self.assertIn('release: ["7.3", "7.4", "7.5", "7.6"]\n', text)

    def test_variant_members_mirror_the_current_list(self):
        # ghostbsd's real matrix lists desktop variants next to the base
        wf = ("jobs:\n  t:\n    strategy:\n      matrix:\n"
              '        release: ["26.1", "26.1-xfce", "26.1-gershwin"]\n'
              '        arch: [""]\n')
        write(".github/workflows/demo.yml", wf)
        index = [rel("26.1"), rel("26.1-xfce", desktop=True),
                 rel("26.1-gershwin", desktop=True),
                 rel("27.0"), rel("27.0-xfce", desktop=True),
                 rel("27.0-gershwin", desktop=True)]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        self.assertIn('release: ["26.1", "26.1-xfce", "26.1-gershwin", '
                      '"27.0", "27.0-xfce", "27.0-gershwin"]',
                      open(".github/workflows/demo.yml").read())

    def test_job_without_arch_axis_is_read_as_x86_64(self):
        # plan9.yml's shape: its own steps instead of testrun.yml, so the
        # matrix carries release + runs and no arch axis at all. Skipping
        # it left plan9 pinned to 11554 while the builder shipped 11952.
        wf = ("jobs:\n  t:\n    strategy:\n      matrix:\n"
              '        release: ["11554"]\n'
              '        runs: ["ubuntu-24.04"]\n')
        write(".github/workflows/demo.yml", wf)
        index = [rel("11554", build=False), rel("11952")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        self.assertIn('release: ["11554", "11952"]',
                      open(".github/workflows/demo.yml").read())
        self.assertTrue(any("no arch axis" in n for n in notes))

    def test_job_without_arch_axis_still_respects_the_index(self):
        # the assumption is x86_64, not "add it regardless": a release
        # published only for another arch stays out
        wf = ("jobs:\n  t:\n    strategy:\n      matrix:\n"
              '        release: ["11554"]\n'
              '        runs: ["ubuntu-24.04"]\n')
        write(".github/workflows/demo.yml", wf)
        index = [rel("11554"), rel("11952", arch="aarch64")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertFalse(changed)
        self.assertIn('release: ["11554"]\n',
                      open(".github/workflows/demo.yml").read())

    def test_missing_workflow_is_reported_not_swallowed(self):
        changed, notes = bv.extend_matrices("nosuchos", [rel("1.0")])
        self.assertFalse(changed)
        self.assertTrue(any("no per-OS workflow" in n for n in notes))

    def test_unsorted_current_list_still_tracks(self):
        wf = ("jobs:\n  t:\n    strategy:\n      matrix:\n"
              '        release: ["26.1", "26.1-xfce"]\n'
              '        arch: [""]\n')
        write(".github/workflows/demo.yml", wf)
        # variant sorts above the base as a string; the job must still
        # count as tracking base 26.1 and receive 27.0
        index = [rel("26.1"), rel("26.1-xfce"), rel("27.0")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        self.assertIn('"27.0"',
                      open(".github/workflows/demo.yml").read())

    def test_point_release_of_older_branch_is_added(self):
        # NetBSD 10.2 was cut after 11.0 (FreeBSD 14.5 after 15.1):
        # comparing only against the job's newest release skipped it
        # without a note, and coverage.yml went red
        wf = ("jobs:\n  test:\n    strategy:\n      matrix:\n"
              '        release: ["9.4", "10.0", "10.1", "11.0"]\n'
              '        arch: ["aarch64", ""]\n'
              "  testriscv64:\n    strategy:\n      matrix:\n"
              '        release: ["11.0"]\n'
              '        arch: ["riscv64"]\n')
        write(".github/workflows/demo.yml", wf)
        index = [rel("11.0", arch="riscv64")]
        for r in ("9.4", "10.0", "10.1", "10.2", "11.0"):
            index += [rel(r), rel(r, arch="aarch64")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        text = open(".github/workflows/demo.yml").read()
        self.assertIn('release: ["9.4", "10.0", "10.1", "10.2", "11.0"]',
                      text)
        # the riscv64 job lists no 10.x: 10.2 is not a candidate there,
        # so it is neither added nor noted
        self.assertIn('release: ["11.0"]\n', text)
        self.assertFalse(any("10.2" in n for n in notes))

    def test_older_branch_never_revived_or_backfilled(self):
        # 9.5 is on a branch the job does not list, 10.0 is below the
        # job's newest 10.x: both gaps are hand-curated
        wf = ("jobs:\n  t:\n    strategy:\n      matrix:\n"
              '        release: ["10.1", "11.0"]\n'
              '        arch: [""]\n')
        write(".github/workflows/demo.yml", wf)
        index = [rel("9.5"), rel("10.0"), rel("10.1"), rel("11.0")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertFalse(changed)
        self.assertIn('release: ["10.1", "11.0"]\n',
                      open(".github/workflows/demo.yml").read())

    def test_point_release_and_new_major_keep_release_order(self):
        wf = ("jobs:\n  t:\n    strategy:\n      matrix:\n"
              '        release: ["25.2", "25.2-xfce", "26.1", "26.1-xfce"]\n'
              '        arch: [""]\n')
        write(".github/workflows/demo.yml", wf)
        index = [rel("25.2"), rel("25.2-xfce", desktop=True),
                 rel("25.3"), rel("25.3-xfce", desktop=True),
                 rel("26.1"), rel("26.1-xfce", desktop=True),
                 rel("26.2"), rel("26.2-xfce", desktop=True)]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        self.assertIn('release: ["25.2", "25.2-xfce", "25.3", "25.3-xfce", '
                      '"26.1", "26.1-xfce", "26.2", "26.2-xfce"]',
                      open(".github/workflows/demo.yml").read())

    def test_variant_only_job_gains_variants_only(self):
        # netbsd's testmicrovm lists 11.0-microvm and no plain 11.0; the
        # 2.2.8 bump (a75e3bf) put a plain "11.1" into it
        wf = ("jobs:\n  test:\n    strategy:\n      matrix:\n"
              '        release: ["10.2", "11.0"]\n'
              '        arch: [""]\n'
              "  testmicrovm:\n    strategy:\n      matrix:\n"
              '        release: ["11.0-microvm"]\n'
              '        arch: [""]\n')
        write(".github/workflows/demo.yml", wf)
        index = [rel("10.2"), rel("11.0"), rel("11.0-microvm"),
                 rel("11.1"), rel("11.1-microvm")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        text = open(".github/workflows/demo.yml").read()
        self.assertIn('release: ["10.2", "11.0", "11.1"]\n', text)
        self.assertIn('release: ["11.0-microvm", "11.1-microvm"]\n', text)
        self.assertEqual(notes, [])

    def test_variant_only_job_stays_so_after_its_base_is_retired(self):
        # 11.0 no longer built (build:false, as 9.x is in netbsd 2.2.8):
        # 11.0-microvm must still read as a variant, not a bare release
        wf = ("jobs:\n  testmicrovm:\n    strategy:\n      matrix:\n"
              '        release: ["11.0-microvm", "11.1-microvm"]\n'
              '        arch: [""]\n')
        write(".github/workflows/demo.yml", wf)
        index = [rel("11.0", build=False), rel("11.0-microvm"),
                 rel("11.1"), rel("11.1-microvm"),
                 rel("11.2"), rel("11.2-microvm")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        self.assertIn('release: ["11.0-microvm", "11.1-microvm", '
                      '"11.2-microvm"]\n',
                      open(".github/workflows/demo.yml").read())

    def test_variant_only_job_without_new_variant_is_untouched(self):
        wf = ("jobs:\n  test:\n    strategy:\n      matrix:\n"
              '        release: ["11.0"]\n'
              '        arch: [""]\n'
              "  testmicrovm:\n    strategy:\n      matrix:\n"
              '        release: ["11.0-microvm"]\n'
              '        arch: [""]\n')
        write(".github/workflows/demo.yml", wf)
        index = [rel("11.0"), rel("11.0-microvm"), rel("11.1")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        text = open(".github/workflows/demo.yml").read()
        self.assertIn('release: ["11.0", "11.1"]\n', text)
        self.assertIn('release: ["11.0-microvm"]\n', text)
        self.assertTrue(any("11.1-microvm" in n for n in notes))

    def test_sync_exclude_of_a_listed_release_is_noted(self):
        # netbsd's testsparc64 excludes 11.0 x rsync (that image ships no
        # rsync); 11.1-sparc64 has none either, which no index field says
        wf_path = ".github/workflows/demo.yml"
        write(wf_path,
              "jobs:\n  testsparc64:\n    strategy:\n      matrix:\n"
              '        release: ["10.2", "11.0"]\n'
              '        arch: ["sparc64"]\n'
              '        sync: ["scp", "rsync"]\n'
              "        exclude:\n"
              "          # no rsync binary on 11.0\n"
              '          - release: "11.0"\n'
              '            sync: "rsync"  # see the conf\n'
              "\n"
              "    uses: ./.github/workflows/testrun.yml\n")
        before = open(wf_path).read()
        index = [rel(r, arch="sparc64") for r in ("10.2", "11.0", "11.1")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        after = open(wf_path).read()
        self.assertEqual(after, before.replace('["10.2", "11.0"]',
                                               '["10.2", "11.0", "11.1"]'))
        hits = [n for n in notes if "exclude" in n]
        self.assertEqual(len(hits), 1)
        self.assertIn("demo.yml:5:", hits[0])
        self.assertIn("added 11.1", hits[0])
        self.assertIn("release=11.0 sync=rsync", hits[0])

    def test_exclude_already_naming_the_new_release_is_not_noted(self):
        write(".github/workflows/demo.yml",
              "jobs:\n  t:\n    strategy:\n      matrix:\n"
              '        release: ["11.0"]\n'
              '        arch: ["sparc64"]\n'
              "        exclude:\n"
              '          - release: "11.0"\n'
              "            sync: rsync\n"
              '          - release: "11.1"\n'
              "            sync: rsync\n")
        index = [rel(r, arch="sparc64") for r in ("11.0", "11.1")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        self.assertFalse(any("exclude" in n for n in notes))

    def test_arch_only_exclude_is_not_noted(self):
        # alpine excludes 3.23 x riscv64 (no such image); a new release
        # is only added when it ships every arch, so nothing to check
        write(".github/workflows/demo.yml",
              "jobs:\n  test:\n    strategy:\n      matrix:\n"
              '        release: ["3.23", "3.24"]\n'
              '        arch: ["", "riscv64"]\n'
              "        exclude:\n"
              '          - release: "3.23"\n'
              "            arch: riscv64\n"
              "          - runs: ubuntu-26.04-arm\n"
              '            sync: "nfs"\n'
              "\n"
              "    uses: ./.github/workflows/testrun.yml\n")
        index = [rel("3.23"), rel("3.24"), rel("3.24", arch="riscv64"),
                 rel("3.25"), rel("3.25", arch="riscv64")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        self.assertIn('release: ["3.23", "3.24", "3.25"]',
                      open(".github/workflows/demo.yml").read())
        self.assertEqual(notes, [])

    def test_exclude_parsing_stops_at_the_matrix_end(self):
        lines = ("jobs:\n  a:\n    strategy:\n      matrix:\n"
                 '        release: ["1.0"]\n'
                 "        exclude:\n"
                 '          - release: "1.0"\n'
                 "            sync: nfs\n"
                 "    uses: x\n"
                 "  b:\n    strategy:\n      matrix:\n"
                 '        release: ["2.0"]\n'
                 "        exclude:\n"
                 '          - release: "2.0"\n'
                 "            sync: tar\n").splitlines(True)
        self.assertEqual(bv._matrix_excludes(lines, 4, "        "),
                         [{"release": "1.0", "sync": "nfs"}])
        self.assertEqual(bv._matrix_excludes(lines, 12, "        "),
                         [{"release": "2.0", "sync": "tar"}])

    def test_variant_only_job_tracks_when_its_only_base_is_retired(self):
        # with 11.0 build:false, 11.0-microvm must still resolve to base
        # 11.0; read as a base of its own it mirrored no suffix and the
        # job silently stopped tracking
        path = ".github/workflows/demo.yml"
        wf = ("jobs:\n  testmicrovm:\n    strategy:\n      matrix:\n"
              '        release: ["11.0-microvm"]\n'
              '        arch: [""]\n')
        write(path, wf)
        index = [rel("11.0", build=False), rel("11.0-microvm"),
                 rel("11.1"), rel("11.1-microvm")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        self.assertEqual(open(path).read(),
                         wf.replace('["11.0-microvm"]',
                                    '["11.0-microvm", "11.1-microvm"]'))
        self.assertEqual(notes, [])

    def test_variant_only_point_release_needs_no_bare_image(self):
        # the job runs only the variant, so only the variant must ship on
        # its arch; the bare 10.2 exists on x86_64 alone
        path = ".github/workflows/demo.yml"
        wf = ("jobs:\n  t:\n    strategy:\n      matrix:\n"
              '        release: ["10.1-microvm", "11.0-microvm"]\n'
              '        arch: ["aarch64"]\n')
        write(path, wf)
        index = [rel("10.1"), rel("10.2"), rel("11.0"),
                 rel("10.1-microvm", arch="aarch64"),
                 rel("10.2-microvm", arch="aarch64"),
                 rel("11.0-microvm", arch="aarch64")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        self.assertEqual(
            open(path).read(),
            wf.replace('["10.1-microvm", "11.0-microvm"]',
                       '["10.1-microvm", "10.2-microvm", "11.0-microvm"]'))
        self.assertEqual(notes, [])

    def test_bare_release_naming_schemes(self):
        # a hyphen alone does not make a variant: openEuler's releases
        # are hyphenated bases
        cases = [
            (["22.03-LTS-SP4", "24.03-LTS-SP4", "26.09"], "24.03-LTS-SP5",
             ["22.03-LTS-SP4", "24.03-LTS-SP4", "24.03-LTS-SP5", "26.09"]),
            (["r151058", "r151060"], "r151062",
             ["r151058", "r151060", "r151062"]),
            (["11554", "11952"], "12000", ["11554", "11952", "12000"]),
        ]
        path = ".github/workflows/demo.yml"
        for current, new, expected in cases:
            with self.subTest(new=new):
                wf = ("jobs:\n  t:\n    strategy:\n      matrix:\n"
                      "        release: %s\n"
                      '        arch: [""]\n') % json.dumps(current)
                write(path, wf)
                changed, notes = bv.extend_matrices(
                    "demo", [rel(r) for r in current + [new]])
                self.assertTrue(changed)
                self.assertEqual(open(path).read(),
                                 wf.replace(json.dumps(current),
                                            json.dumps(expected)))
                self.assertEqual(notes, [])

    def test_variant_only_job_needs_no_built_bare_base(self):
        path = ".github/workflows/demo.yml"
        wf = ("jobs:\n  t:\n    strategy:\n      matrix:\n"
              '        release: ["11.0-microvm"]\n'
              '        arch: [""]\n')
        write(path, wf)
        index = [rel("11.0"), rel("11.0-microvm"),
                 rel("11.1", build=False), rel("11.1-microvm")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        self.assertEqual(open(path).read(),
                         wf.replace('["11.0-microvm"]',
                                    '["11.0-microvm", "11.1-microvm"]'))
        self.assertEqual(notes, [])

    def test_variant_only_job_still_checks_every_arch(self):
        # skipping the bare-image check must keep the variant's own
        path = ".github/workflows/demo.yml"
        wf = ("jobs:\n  t:\n    strategy:\n      matrix:\n"
              '        release: ["11.0-microvm"]\n'
              '        arch: ["", "aarch64"]\n')
        write(path, wf)
        index = [rel("11.0"), rel("11.1"),
                 rel("11.0-microvm"), rel("11.0-microvm", arch="aarch64"),
                 rel("11.1-microvm")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertFalse(changed)
        self.assertEqual(open(path).read(), wf)
        self.assertTrue(any("11.1-microvm" in n and "aarch64" in n
                            for n in notes))

    def test_second_run_is_a_noop(self):
        path = ".github/workflows/demo.yml"
        write(path,
              "jobs:\n  t:\n    strategy:\n      matrix:\n"
              '        release: ["11.0-microvm"]\n'
              '        arch: [""]\n'
              "        exclude:\n"
              '          - release: "11.0-microvm"\n'
              "            sync: rsync\n")
        index = [rel("11.0"), rel("11.0-microvm"),
                 rel("11.1"), rel("11.1-microvm")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        self.assertEqual(len([n for n in notes if "exclude" in n]), 1)
        after = open(path).read()
        self.assertIn('release: ["11.0-microvm", "11.1-microvm"]\n', after)
        self.assertEqual(bv.extend_matrices("demo", index), (False, []))
        self.assertEqual(open(path).read(), after)

    def test_sync_exclude_before_the_release_axis_is_noted(self):
        path = ".github/workflows/demo.yml"
        wf = ("jobs:\n  testsparc64:\n    strategy:\n      matrix:\n"
              "        exclude:\n"
              '          - release: "11.0"\n'
              "            sync: rsync\n"
              '        release: ["11.0"]\n'
              '        arch: ["sparc64"]\n'
              '        sync: ["scp", "rsync"]\n')
        write(path, wf)
        index = [rel("11.0", arch="sparc64"), rel("11.1", arch="sparc64")]
        changed, notes = bv.extend_matrices("demo", index)
        self.assertTrue(changed)
        self.assertEqual(open(path).read(),
                         wf.replace('["11.0"]', '["11.0", "11.1"]'))
        hits = [n for n in notes if "exclude" in n]
        self.assertEqual(len(hits), 1)
        self.assertIn("release=11.0 sync=rsync", hits[0])

    def test_indentless_exclude_sequence_is_parsed(self):
        lines = ("jobs:\n  testsparc64:\n    strategy:\n      matrix:\n"
                 '        release: ["11.0"]\n'
                 '        arch: ["sparc64"]\n'
                 '        sync: ["scp", "rsync"]\n'
                 "        exclude:\n"
                 '        - release: "11.0"\n'
                 "          sync: rsync\n"
                 "        include:\n"
                 '        - release: "99.0"\n'
                 "          sync: nfs\n").splitlines(True)
        self.assertEqual(bv._matrix_excludes(lines, 4, "        "),
                         [{"release": "11.0", "sync": "rsync"}])

    def test_standalone_sequence_marker_is_parsed(self):
        lines = ("jobs:\n  t:\n    strategy:\n      matrix:\n"
                 '        release: ["11.0"]\n'
                 "        exclude:\n"
                 "          -\n"
                 '            release: "11.0"\n'
                 "            sync: rsync\n").splitlines(True)
        self.assertEqual(bv._matrix_excludes(lines, 4, "        "),
                         [{"release": "11.0", "sync": "rsync"}])

    def test_empty_exclude_does_not_take_the_include_entries(self):
        lines = ("      matrix:\n"
                 '        release: ["11.0"]\n'
                 "        exclude: []\n"
                 "        include:\n"
                 '          - release: "11.0"\n'
                 "            sync: rsync\n").splitlines(True)
        self.assertEqual(bv._matrix_excludes(lines, 1, "        "), [])

    def test_crlf_exclude_and_include_keep_every_other_byte(self):
        path = ".github/workflows/demo.yml"
        wf = ("jobs:\n  t:\n    strategy:\n      matrix:\n"
              '        release: ["11.0"]\n'
              '        arch: ["sparc64"]\n'
              '        sync: ["scp", "rsync"]\n'
              "        exclude:\n"
              "          # image has no rsync\n"
              '          - release: "11.0"\n'
              "            sync: 'rsync'  # keep this comment\n"
              "\n"
              "        include:\n"
              '          - release: "99.0"\n'
              "            sync: nfs\n"
              "    uses: ./.github/workflows/testrun.yml\n"
              ).replace("\n", "\r\n")
        write(path, wf)
        changed, notes = bv.extend_matrices(
            "demo", [rel("11.0", arch="sparc64"),
                     rel("11.1", arch="sparc64")])
        self.assertTrue(changed)
        with open(path, "rb") as source:
            self.assertEqual(source.read(),
                             wf.replace('["11.0"]', '["11.0", "11.1"]')
                             .encode("ascii"))
        self.assertEqual(bv._matrix_excludes(wf.splitlines(True), 4,
                                             "        "),
                         [{"release": "11.0", "sync": "rsync"}])
        self.assertEqual(len([n for n in notes if "exclude" in n]), 1)


class TestAllowMirror(Case):
    def test_suffix_mirrored_onto_new_tag(self):
        added = bv.mirror_allow_lines("demo", ["r2", "r2-build"])
        self.assertEqual(added, ["demo r2-build demo.yml"])
        text = open(".github/coverage.allow").read()
        self.assertIn("demo r2-build demo.yml\n", text)

    def test_no_new_exemptions_invented(self):
        added = bv.mirror_allow_lines("demo", ["r2", "r2-gnome"])
        self.assertEqual(added, [])

    def test_existing_line_not_duplicated(self):
        bv.mirror_allow_lines("demo", ["r2-build"])
        again = bv.mirror_allow_lines("demo", ["r2-build"])
        self.assertEqual(again, [])
        text = open(".github/coverage.allow").read()
        self.assertEqual(text.count("demo r2-build demo.yml"), 1)


class Fetch(object):
    def __init__(self, table):
        self.table = table

    def __call__(self, url):
        for k, v in self.table.items():
            if k in url:
                return v
        raise bv.FetchError("HTTP 404")


def uploaded(osname, index, skip=()):
    """The asset listing of a finished release of `index`."""
    return [{"name": "%s-%s%s" % (osname, e["tag"], s), "state": "uploaded"}
            for e in index if e.get("build", True)
            for s in bv.IMAGE_ASSET_SUFFIXES
            if "%s-%s%s" % (osname, e["tag"], s) not in skip]


def microvm_profile(vm):
    # The shape netbsd-builder's build.py writes for a VM_MICROVM conf.
    return {"machine_type": "microvm", "kernel_asset": vm + "-kernel",
            "kernel_append": "root=dk0 console=com rw"}


class MainCase(Case):
    def _latest(self, tag):
        return json.dumps({"tag_name": tag}).encode()

    def _index(self, entries):
        return json.dumps({"os": "demo", "releases": entries}).encode()

    def _fetch(self, index, assets=None, profiles=None):
        """demo-builder v2.0.1 with `index`; its release carries every
        image file unless `assets` (the raw listing) says otherwise, and
        every profile.json is a plain pc one unless `profiles` (file name
        -> body; a dict is JSON-encoded) says otherwise."""
        if assets is None:
            assets = uploaded("demo", index)
        table = {
            "demo-builder/releases/latest": self._latest("v2.0.1"),
            "v2.0.1/releases.json": self._index(index),
            "demo-builder/releases/tags/v2.0.1": json.dumps(
                {"id": 7}).encode(),
            "releases/7/assets": json.dumps(assets).encode(),
        }
        for name, body in (profiles or {}).items():
            table[name] = (json.dumps(body).encode()
                           if isinstance(body, dict) else body)
        # Last, so the specific profiles above win (Fetch matches keys in
        # insertion order).
        table[".profile.json"] = json.dumps({"machine_type": "q35"}).encode()
        return Fetch(table)


class TestMain(MainCase):
    def test_noop_when_current(self):
        write("anyvm.py", 'DEFAULT_BUILDER_VERSIONS = {\n'
              '    "demo": "2.0.0",\n}\n')
        fetch = Fetch({"demo-builder/releases/latest": self._latest("v2.0.0")})
        self.assertEqual(bv.main(["--check"], fetch=fetch), 0)

    def test_bump_and_matrix_and_allow(self):
        write("anyvm.py", 'DEFAULT_BUILDER_VERSIONS = {\n'
              '    "demo": "2.0.0",\n}\n')
        index = [rel("15.0"), rel("15.0", arch="aarch64"),
                 rel("15.1"), rel("15.1", arch="aarch64"),
                 rel("15.1", arch="riscv64"),
                 rel("15.2"), rel("15.2", arch="aarch64"),
                 rel("15.2", arch="riscv64")]
        fetch = self._fetch(index)
        self.assertEqual(bv.main([], fetch=fetch), 0)
        self.assertIn('"demo": "2.0.1"', open("anyvm.py").read())
        self.assertIn("15.2", open(".github/workflows/demo.yml").read())

    def test_missing_index_leaves_everything(self):
        write("anyvm.py", 'DEFAULT_BUILDER_VERSIONS = {\n'
              '    "demo": "2.0.0",\n}\n')
        fetch = Fetch({"demo-builder/releases/latest": self._latest("v2.0.1")})
        rc = bv.main([], fetch=fetch)
        self.assertEqual(rc, 1)
        self.assertIn('"demo": "2.0.0"', open("anyvm.py").read())

    def test_landed_out_written_on_bump(self):
        write("anyvm.py", 'DEFAULT_BUILDER_VERSIONS = {\n'
              '    "demo": "2.0.0",\n}\n')
        index = [rel("15.0"), rel("15.0", arch="aarch64"),
                 rel("15.1"), rel("15.1", arch="aarch64"),
                 rel("15.1", arch="riscv64")]
        fetch = self._fetch(index)
        rc = bv.main(["--landed-out", "landed.txt"], fetch=fetch)
        self.assertEqual(rc, 0)
        self.assertIn("demo 2.0.0 -> 2.0.1",
                      open("landed.txt", encoding="utf-8").read())

    def test_landed_out_absent_on_noop_and_check(self):
        write("anyvm.py", 'DEFAULT_BUILDER_VERSIONS = {\n'
              '    "demo": "2.0.0",\n}\n')
        fetch = Fetch({"demo-builder/releases/latest": self._latest("v2.0.0")})
        self.assertEqual(bv.main(["--landed-out", "landed.txt"],
                                 fetch=fetch), 0)
        self.assertFalse(os.path.exists("landed.txt"))
        fetch2 = self._fetch([rel("15.1")])
        self.assertEqual(bv.main(["--check", "--landed-out",
                                  "landed.txt"], fetch=fetch2), 0)
        self.assertFalse(os.path.exists("landed.txt"))

    def test_notes_out_written_on_bump(self):
        write("anyvm.py", 'DEFAULT_BUILDER_VERSIONS = {\n'
              '    "demo": "2.0.0",\n}\n')
        # 15.2 ships x86_64 + aarch64 but not riscv64, so the riscv64 job
        # refuses it and says why -- exactly what belongs in the issue.
        index = [rel("15.1"), rel("15.1", arch="aarch64"),
                 rel("15.1", arch="riscv64"),
                 rel("15.2"), rel("15.2", arch="aarch64")]
        fetch = self._fetch(index)
        rc = bv.main(["--notes-out", "notes.txt"], fetch=fetch)
        self.assertEqual(rc, 0)
        self.assertIn("riscv64", open("notes.txt", encoding="utf-8").read())

    def test_notes_out_absent_on_check(self):
        write("anyvm.py", 'DEFAULT_BUILDER_VERSIONS = {\n'
              '    "demo": "2.0.0",\n}\n')
        fetch = self._fetch([rel("15.2")])
        self.assertEqual(bv.main(["--check", "--notes-out", "notes.txt"],
                                 fetch=fetch), 0)
        self.assertFalse(os.path.exists("notes.txt"))

    def test_check_writes_nothing(self):
        write("anyvm.py", 'DEFAULT_BUILDER_VERSIONS = {\n'
              '    "demo": "2.0.0",\n}\n')
        index = [rel("15.0"), rel("15.0", arch="aarch64"),
                 rel("15.1"), rel("15.1", arch="aarch64"),
                 rel("15.1", arch="riscv64")]
        fetch = self._fetch(index)
        self.assertEqual(bv.main(["--check"], fetch=fetch), 0)
        self.assertIn('"demo": "2.0.0"', open("anyvm.py").read())


class TestImageGate(MainCase):
    """A release is bumped to only once every built image is uploaded:
    releases.json lands first, the images up to hours later."""

    def setUp(self):
        super().setUp()
        write("anyvm.py", 'DEFAULT_BUILDER_VERSIONS = {\n'
              '    "demo": "2.0.0",\n}\n')
        self.wf = os.path.join(".github", "workflows", "demo.yml")
        self.before = open(self.wf, encoding="utf-8").read()
        # Real index shape: one tag (= one image file set) per arch.
        self.index = [rel("15.1"), rel("15.1-aarch64", "15.1", "aarch64"),
                      rel("15.1-riscv64", "15.1", "riscv64"),
                      rel("15.2"), rel("15.2-aarch64", "15.2", "aarch64"),
                      rel("15.2-riscv64", "15.2", "riscv64")]

    def _run(self, fetch):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = bv.main(["--landed-out", "landed.txt"], fetch=fetch)
        return rc, err.getvalue()

    def assertUntouched(self):
        self.assertIn('"demo": "2.0.0"', open("anyvm.py").read())
        self.assertEqual(open(self.wf, encoding="utf-8").read(), self.before)
        self.assertNotIn("15.2", open(".github/coverage.allow").read())
        self.assertFalse(os.path.exists("landed.txt"))

    def test_image_not_yet_uploaded_leaves_everything(self):
        # The anyvm run 41601403051 shape: releases.json is up, one
        # image's files are not.
        assets = uploaded("demo", self.index,
                          skip={"demo-15.2.qcow2.zst", "demo-15.2.qemu"})
        rc, err = self._run(self._fetch(self.index, assets))
        self.assertEqual(rc, 1)
        self.assertIn("demo-15.2.qcow2.zst", err)
        self.assertIn("demo-15.2.qemu", err)
        self.assertUntouched()

    def test_open_asset_is_not_uploaded(self):
        assets = uploaded("demo", self.index)
        for a in assets:
            if a["name"] == "demo-15.2.profile.json":
                a["state"] = "open"
        rc, err = self._run(self._fetch(self.index, assets))
        self.assertEqual(rc, 1)
        self.assertIn("demo-15.2.profile.json", err)
        self.assertUntouched()

    def test_unlistable_assets_leave_everything(self):
        fetch = self._fetch(self.index)
        del fetch.table["demo-builder/releases/tags/v2.0.1"]
        rc, err = self._run(fetch)
        self.assertEqual(rc, 1)
        self.assertIn("cannot list demo v2.0.1 assets", err)
        self.assertUntouched()

    def test_check_reports_incomplete_release(self):
        assets = uploaded("demo", self.index, skip={"demo-15.2.qcow2.zst"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = bv.main(["--check"], fetch=self._fetch(self.index, assets))
        self.assertEqual(rc, 1)
        self.assertIn("demo-15.2.qcow2.zst", err.getvalue())

    def test_unbuilt_entries_need_no_images(self):
        # build:false tags keep their images at older builder tags.
        index = self.index + [rel("14.3", build=False)]
        rc, _ = self._run(self._fetch(index, uploaded("demo", self.index)))
        self.assertEqual(rc, 0)
        self.assertIn('"demo": "2.0.1"', open("anyvm.py").read())

    def test_split_parts_and_extra_assets_are_fine(self):
        assets = uploaded("demo", self.index) + [
            {"name": "demo-15.2.qcow2.zst.1", "state": "uploaded"},
            {"name": "releases.json", "state": "uploaded"}]
        rc, _ = self._run(self._fetch(self.index, assets))
        self.assertEqual(rc, 0)

    def test_assets_listed_across_pages(self):
        # 100 = a full page, so the gate must ask for page 2, where the
        # last image file is.
        assets = uploaded("demo", self.index)
        filler = [{"name": "filler-%d" % i, "state": "uploaded"}
                  for i in range(bv.ASSETS_PER_PAGE - len(assets) + 1)]
        listing = assets[:-1] + filler
        page1, page2 = (listing[:bv.ASSETS_PER_PAGE],
                        listing[bv.ASSETS_PER_PAGE:] + assets[-1:])
        fetch = self._fetch(self.index)
        del fetch.table["releases/7/assets"]
        fetch.table["releases/7/assets?per_page=100&page=1"] = \
            json.dumps(page1).encode()
        fetch.table["releases/7/assets?per_page=100&page=2"] = \
            json.dumps(page2).encode()
        rc, err = self._run(fetch)
        self.assertEqual(len(page1), bv.ASSETS_PER_PAGE)
        self.assertEqual(rc, 0, err)

    def test_exact_full_page_asks_for_the_empty_next_page(self):
        calls = []
        assets = [{"name": "asset-%d" % i, "state": "uploaded"}
                  for i in range(bv.ASSETS_PER_PAGE)]
        root = bv.API + "/repos/anyvm-org/demo-builder/releases"
        responses = {
            root + "/tags/v2.0.1": {"id": 7},
            root + "/7/assets?per_page=100&page=1": assets,
            root + "/7/assets?per_page=100&page=2": [],
        }

        def fetch(url):
            calls.append(url)
            return json.dumps(responses[url]).encode("utf-8")

        names = bv.uploaded_asset_names("demo", "2.0.1", fetch)
        self.assertEqual(names, {a["name"] for a in assets})
        self.assertEqual(calls, list(responses))

    def test_later_page_failure_leaves_everything(self):
        # Page 1 alone holds every image file; the bump must still wait
        # for the whole listing.
        assets = uploaded("demo", self.index)
        assets += [{"name": "filler-%d" % i, "state": "uploaded"}
                   for i in range(bv.ASSETS_PER_PAGE - len(assets))]
        base = self._fetch(self.index)
        root = bv.API + "/repos/anyvm-org/demo-builder/releases/7/assets"

        def fetch(url):
            if url == root + "?per_page=100&page=1":
                return json.dumps(assets).encode("utf-8")
            if url == root + "?per_page=100&page=2":
                raise bv.FetchError("HTTP 503")
            return base(url)

        rc, err = self._run(fetch)
        self.assertEqual(rc, 1)
        self.assertIn("cannot list demo v2.0.1 assets (HTTP 503)", err)
        self.assertUntouched()

    def test_desktop_image_needs_the_five_upload_names(self):
        # Literal names, not IMAGE_ASSET_SUFFIXES: uploaded() derives its
        # fixtures from that constant, so dropping a suffix from it would
        # otherwise go unnoticed.
        index = [rel("27.0-xfce", "27.0", desktop=True)]
        expected = {"demo-27.0-xfce.qcow2.zst", "demo-27.0-xfce.qemu",
                    "demo-27.0-xfce.profile.json",
                    "demo-27.0-xfce-id_rsa.pub", "demo-27.0-xfce-host.id_rsa"}
        self.assertEqual(set(bv.missing_image_assets("demo", index, set())),
                         expected)
        self.assertEqual(bv.missing_image_assets("demo", index, expected), [])

    def _microvm(self, kernel_state="uploaded", profile=None):
        """self.index plus 15.2-microvm, every image file uploaded; the
        kernel asset only when kernel_state is not None."""
        index = self.index + [rel("15.2-microvm")]
        assets = uploaded("demo", index)
        if kernel_state:
            assets.append({"name": "demo-15.2-microvm-kernel",
                           "state": kernel_state})
        return index, self._fetch(index, assets, profiles={
            "demo-15.2-microvm.profile.json":
                profile or microvm_profile("demo-15.2-microvm")})

    def test_microvm_kernel_missing_leaves_everything(self):
        # netbsd v2.2.8: every image file up, the 11.1-microvm kernel not.
        _, fetch = self._microvm(kernel_state=None)
        rc, err = self._run(fetch)
        self.assertEqual(rc, 1)
        self.assertIn("lacks the microvm kernel(s) its profiles name: "
                      "demo-15.2-microvm-kernel", err)
        self.assertUntouched()

    def test_microvm_kernel_still_uploading(self):
        _, fetch = self._microvm(kernel_state="open")
        rc, err = self._run(fetch)
        self.assertEqual(rc, 1)
        self.assertIn("demo-15.2-microvm-kernel", err)
        self.assertUntouched()

    def test_microvm_kernel_present_bumps(self):
        _, fetch = self._microvm()
        rc, err = self._run(fetch)
        self.assertEqual(rc, 0, err)
        self.assertIn('"demo": "2.0.1"', open("anyvm.py").read())

    def test_microvm_profile_without_kernel_fields(self):
        # A refused run writes nothing, so the subtests share one tree.
        # A non-string value is as unusable as a missing one, and must be
        # refused rather than crash the set lookup / join.
        cases = [(field, value)
                 for field in ("kernel_asset", "kernel_append")
                 for value in (None, "", 17, ["demo-15.2-microvm-kernel"])]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                profile = microvm_profile("demo-15.2-microvm")
                if value is None:
                    del profile[field]
                else:
                    profile[field] = value
                _, fetch = self._microvm(profile=profile)
                rc, err = self._run(fetch)
                self.assertEqual(rc, 1)
                self.assertIn("demo-15.2-microvm.profile.json (microvm "
                              "without kernel_asset/kernel_append)", err)
                self.assertUntouched()

    def test_bad_kernel_asset_does_not_block_other_os(self):
        # Before the type check a list here raised TypeError out of
        # main(), so zeta never bumped.
        write("anyvm.py", 'DEFAULT_BUILDER_VERSIONS = {\n'
              '    "demo": "2.0.0",\n'
              '    "zeta": "3.0.0",\n}\n')
        profile = microvm_profile("demo-15.2-microvm")
        profile["kernel_asset"] = ["demo-15.2-microvm-kernel"]
        _, fetch = self._microvm(profile=profile)
        zindex = [rel("1.0")]
        fetch.table.update({
            "zeta-builder/releases/latest": self._latest("v3.0.1"),
            "zeta-builder/releases/download/v3.0.1/releases.json":
                json.dumps({"os": "zeta", "releases": zindex}).encode(),
            "zeta-builder/releases/tags/v3.0.1": json.dumps(
                {"id": 9}).encode(),
            "releases/9/assets": json.dumps(
                uploaded("zeta", zindex)).encode(),
        })
        rc, err = self._run(fetch)
        self.assertEqual(rc, 1, err)
        text = open("anyvm.py").read()
        self.assertIn('"demo": "2.0.0"', text)
        self.assertIn('"zeta": "3.0.1"', text)
        self.assertEqual(open("landed.txt", encoding="utf-8").read(),
                         "zeta 3.0.0 -> 3.0.1\n")

    def test_kernel_check_reads_exact_profile_urls(self):
        # Exact URLs, so the fixtures' substring catch-all cannot hide a
        # wrong one; build:false rows and an empty index fetch nothing.
        root = ("https://github.com/anyvm-org/demo-builder/releases/"
                "download/v2.0.1/")
        responses = {
            root + "demo-15.2.profile.json": {"machine_type": "q35"},
            root + "demo-15.2-microvm.profile.json":
                microvm_profile("demo-15.2-microvm"),
        }
        calls = []

        def fetch(url):
            calls.append(url)
            return json.dumps(responses[url]).encode("utf-8")

        index = [rel("15.2"), rel("15.2-microvm"),
                 rel("14.3-microvm", build=False)]
        self.assertEqual(bv.missing_kernel_assets(
            "demo", "2.0.1", index, {"demo-15.2-microvm-kernel"}, fetch), [])
        self.assertEqual(calls, list(responses))
        for empty in ([], [rel("14.3-microvm", build=False)]):
            self.assertEqual(bv.missing_kernel_assets(
                "demo", "2.0.1", empty, set(), fetch), [])
        self.assertEqual(calls, list(responses))

    def test_unreadable_profile_leaves_everything(self):
        for body, why in ((b"<html>", "is not JSON"),
                          (b"", "is not JSON"),
                          (b"\xff", "is not JSON"),
                          (b"[]", "is not an object"),
                          (b"null", "is not an object"),
                          (b'"microvm"', "is not an object")):
            with self.subTest(body=body):
                fetch = self._fetch(self.index, profiles={
                    "demo-15.2.profile.json": body})
                rc, err = self._run(fetch)
                self.assertEqual(rc, 1)
                self.assertIn("cannot read demo v2.0.1 profiles "
                              "(demo-15.2.profile.json %s" % why, err)
                self.assertUntouched()

    def test_profile_download_failure_leaves_everything(self):
        fetch = self._fetch(self.index)

        def failing(url):
            if url.endswith("/demo-15.2.profile.json"):
                raise bv.FetchError("HTTP 502")
            return fetch(url)

        rc, err = self._run(failing)
        self.assertEqual(rc, 1)
        self.assertIn("cannot read demo v2.0.1 profiles "
                      "(demo-15.2.profile.json: HTTP 502)", err)
        self.assertUntouched()

    def test_unbuilt_microvm_entry_is_not_read(self):
        # A build:false row has no images at this tag; its profile would
        # 404 (here: would not parse) and must never be fetched.
        index = self.index + [rel("14.3-microvm", build=False)]
        fetch = self._fetch(index, uploaded("demo", self.index), profiles={
            "demo-14.3-microvm.profile.json": b"<html>"})
        rc, err = self._run(fetch)
        self.assertEqual(rc, 0, err)

    def test_complete_os_still_bumps_beside_an_incomplete_one(self):
        # omnios (still uploading) must not hold back openindiana.
        write("anyvm.py", 'DEFAULT_BUILDER_VERSIONS = {\n'
              '    "demo": "2.0.0",\n'
              '    "zeta": "3.0.0",\n}\n')
        zindex = [rel("1.0")]
        fetch = self._fetch(self.index, uploaded(
            "demo", self.index, skip={"demo-15.2.qcow2.zst"}))
        fetch.table.update({
            "zeta-builder/releases/latest": self._latest("v3.0.1"),
            "zeta-builder/releases/download/v3.0.1/releases.json":
                json.dumps({"os": "zeta", "releases": zindex}).encode(),
            "zeta-builder/releases/tags/v3.0.1": json.dumps(
                {"id": 9}).encode(),
            "releases/9/assets": json.dumps(
                uploaded("zeta", zindex)).encode(),
        })
        rc, _ = self._run(fetch)
        self.assertEqual(rc, 1)
        text = open("anyvm.py").read()
        self.assertIn('"demo": "2.0.0"', text)
        self.assertIn('"zeta": "3.0.1"', text)
        self.assertEqual(open("landed.txt", encoding="utf-8").read(),
                         "zeta 3.0.0 -> 3.0.1\n")


if __name__ == "__main__":
    unittest.main()
