"""
Tests for seaworthy.ps module.

Please note that these are "core" tests and thus may not depend on anything
that isn't already a non-optional dependency of Seaworthy itself.
"""

import unittest

from seaworthy.ps import PsException, PsRow, PsTree, build_process_tree


def mkrow(pid, ppid, ruser='root', args=None):
    if args is None:
        args = 'args for pid {}'.format(pid)
    return PsRow(pid, ppid, ruser, args)


class TestPsRow(unittest.TestCase):
    def test_columns(self):
        """
        The PsRow class knows what columns it requires from ps.
        """
        self.assertEqual(PsRow.columns(), ['pid', 'ppid', 'ruser', 'args'])

    def test_fields(self):
        """
        A PsRow can be created from field values of various types.
        """
        ps_row = PsRow('1', '0', 'root', 'tini -- true')
        self.assertEqual(
            (ps_row.pid, ps_row.ppid, ps_row.ruser, ps_row.args),
            (1, 0, 'root', 'tini -- true'))

        ps_row = PsRow(1, 0, 'root', 'tini -- true')
        self.assertEqual(
            (ps_row.pid, ps_row.ppid, ps_row.ruser, ps_row.args),
            (1, 0, 'root', 'tini -- true'))


class TestPsTree(unittest.TestCase):
    def test_count(self):
        """
        A PsTree knows how many entries it contains.
        """
        self.assertEqual(1, PsTree(mkrow(1, 0)).count())

        self.assertEqual(3, PsTree(mkrow(1, 0), [
            PsTree(mkrow(6, 1), [
                PsTree(mkrow(8, 6)),
            ]),
        ]).count())

        self.assertEqual(6, PsTree(mkrow(1, 0), [
            PsTree(mkrow(6, 1), [
                PsTree(mkrow(8, 6)),
            ]),
            PsTree(mkrow(9, 1), [
                PsTree(mkrow(11, 9)),
                PsTree(mkrow(12, 9)),
            ]),
        ]).count())


class TestBuildProcessTreeFunc(unittest.TestCase):
    def test_single_process(self):
        """
        We can build a PsTree for a single process.
        """
        ps_row = PsRow('1', '0', 'root', 'tini -- echo "hi"')
        ps_tree = build_process_tree([ps_row])

        self.assertEqual(ps_tree, PsTree(ps_row, children=[]))

    def test_simple_tree(self):
        """
        We can build a PsTree for a list of grandparent/parent/child processes.
        """
        ps_rows = [
            mkrow(1, 0, 'root', "tini -- nginx -g 'daemon off;'"),
            mkrow(6, 1, 'root', 'nginx: master process nginx -g daemon off;'),
            mkrow(8, 6, 'nginx', 'nginx: worker process'),
        ]
        ps_tree = build_process_tree(ps_rows)
        self.assertEqual(ps_tree, PsTree(ps_rows[0], [
            PsTree(ps_rows[1], [
                PsTree(ps_rows[2], []),
            ]),
        ]))

    def test_bigger_tree(self):
        """
        We can build a PsTree for a more complicated process list.
        """
        ps_rows = [
            None,  # Dummy entry so list indices match pids.
            mkrow(1, 0),
            mkrow(2, 1),
            mkrow(3, 1),
            mkrow(4, 2),
            mkrow(5, 3),
            mkrow(6, 3),
            mkrow(7, 4),
            mkrow(8, 2),
            mkrow(9, 1),
        ]
        ps_tree = build_process_tree(ps_rows[1:])
        self.assertEqual(ps_tree, PsTree(ps_rows[1], [
            PsTree(ps_rows[2], [
                PsTree(ps_rows[4], [
                    PsTree(ps_rows[7]),
                ]),
                PsTree(ps_rows[8]),
            ]),
            PsTree(ps_rows[3], [
                PsTree(ps_rows[5]),
                PsTree(ps_rows[6]),
            ]),
            PsTree(ps_rows[9]),
        ]))

    def test_no_root_pid(self):
        """
        We can't build a process tree if we don't have a root process.
        """
        with self.assertRaises(PsException) as cm:
            build_process_tree([])
        self.assertIn("No process tree root", str(cm.exception))

        with self.assertRaises(PsException) as cm:
            build_process_tree([
                mkrow(2, 1),
                mkrow(3, 1),
                mkrow(4, 2),
            ])
        self.assertIn("No process tree root", str(cm.exception))

    def test_multiple_root_pids(self):
        """
        We can't build a process tree if we have too many root processes.
        """
        with self.assertRaises(PsException) as cm:
            build_process_tree([
                mkrow(1, 0),
                mkrow(2, 0),
                mkrow(4, 2),
            ])
        self.assertIn("Too many process tree roots", str(cm.exception))

    def test_malformed_process_tree(self):
        """
        We can't build a process tree with disconnected processes.
        """
        with self.assertRaises(PsException) as cm:
            build_process_tree([
                mkrow(1, 0),
                mkrow(2, 1),
                mkrow(4, 3),
            ])
        self.assertIn("Unreachable processes", str(cm.exception))

    def test_duplicate_pids(self):
        """
        We can't build a process tree with duplicate pids.
        """
        with self.assertRaises(PsException) as cm:
            build_process_tree([
                mkrow(1, 0),
                mkrow(2, 1),
                mkrow(2, 1),
                mkrow(3, 2),
            ])
        self.assertIn("Duplicate pid found: 2", str(cm.exception))
