import importlib.util
import os
import sys
import types
import unittest


maya = types.ModuleType("maya")
maya.cmds = types.ModuleType("maya.cmds")
maya.mel = types.ModuleType("maya.mel")
sys.modules.setdefault("maya", maya)
sys.modules.setdefault("maya.cmds", maya.cmds)
sys.modules.setdefault("maya.mel", maya.mel)

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "groomist.py")
SPEC = importlib.util.spec_from_file_location("groomist_test", MODULE_PATH)
groomist = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(groomist)


class FakeCmds(object):
    def __init__(self, typed_nodes, attrs=None, query_error=False,
                 history_error=False):
        self.stack = [node for node, _ in typed_nodes]
        self.node_types = dict(typed_nodes)
        self.attrs = attrs or {}
        self.query_error = query_error
        self.history_error = history_error
        self.warnings = []
        self.selections = []

    def OxGetStackNodes(self, hair):
        if self.query_error:
            raise RuntimeError("stack query failed")
        return list(self.stack)

    def listHistory(self, hair):
        if self.history_error:
            raise RuntimeError("history query failed")
        return list(self.stack)

    def objExists(self, node):
        return node in self.node_types

    def nodeType(self, node):
        return self.node_types[node]

    def select(self, nodes, replace=True):
        self.selections.append(nodes)

    def warning(self, message):
        self.warnings.append(message)

    def attributeQuery(self, attr, node=None, exists=False, **kwargs):
        return attr in self.attrs.get(node, {})

    def getAttr(self, plug):
        node, attr = plug.rsplit(".", 1)
        return self.attrs[node][attr]

    def setAttr(self, plug, value):
        node, attr = plug.rsplit(".", 1)
        self.attrs.setdefault(node, {})[attr] = value

    def addAttr(self, node, longName=None, attributeType=None,
                defaultValue=None):
        self.attrs.setdefault(node, {})[longName] = defaultValue

    def listAttr(self, node, settable=True):
        return list(self.attrs.get(node, {}))

    def add_node(self, node, node_type, attrs=None):
        self.node_types[node] = node_type
        self.attrs[node] = attrs or {}
        self.stack.append(node)

    def remove_node(self, node):
        self.node_types.pop(node, None)
        self.attrs.pop(node, None)
        self.stack = [item for item in self.stack if item != node]


class FakeMel(object):
    def __init__(self, cmds, delete_failure=False):
        self.cmds = cmds
        self.delete_failure = delete_failure

    def eval(self, command):
        if command.startswith("OxDeleteStrandOperator"):
            if self.delete_failure:
                raise RuntimeError("operator delete failed")
            node = command.split('"')[1]
            self.cmds.remove_node(node)


class StackBuildTests(unittest.TestCase):
    def run_build(self, typed_nodes, attrs=None, query_error=False,
                  history_error=False, delete_failure=False, fail_op=None):
        cmds = FakeCmds(
            typed_nodes,
            attrs=attrs,
            query_error=query_error,
            history_error=history_error,
        )
        groomist.cmds = cmds
        groomist.mel = FakeMel(cmds, delete_failure=delete_failure)
        groomist._current_hair = lambda: "hairShape"

        added = []
        messages = []
        counts = {}

        def add_operator(op, enabled=True):
            if op == fail_op:
                return None
            counts[op] = counts.get(op, 0) + 1
            node = "{}New{}".format(op, counts[op])
            node_attrs = {"width": 0.0} if op == "ChangeWidth" else {}
            cmds.add_node(node, groomist.OP_TYPES[op], node_attrs)
            added.append(op)
            return node

        groomist._add_operator = add_operator
        groomist._msg = lambda text, ok=True: messages.append((text, ok))
        groomist.build_full_stack_disabled()
        return cmds, added, messages

    @staticmethod
    def strips_seed(marked=True):
        typed = [
            ("stripsGenerator", "HairFromMeshStripsNode"),
            ("ground", "GroundStrandsNode"),
            ("seedWidth", "ChangeWidthNode"),
        ]
        attrs = {"seedWidth": {"width": 0.12}}
        if marked:
            attrs["seedWidth"][groomist.GROOMIST_WIDTH_ATTR] = True
        return typed, attrs

    def test_managed_seed_is_rebuilt_last_and_preserves_width(self):
        typed, attrs = self.strips_seed(marked=True)
        cmds, added, messages = self.run_build(typed, attrs)

        self.assertEqual(groomist.STACK_ORDER, added)
        self.assertFalse(cmds.objExists("seedWidth"))
        self.assertEqual(0.12, cmds.attrs["ChangeWidthNew1"]["width"])
        self.assertTrue(
            cmds.attrs["ChangeWidthNew1"][groomist.GROOMIST_WIDTH_ATTR]
        )
        self.assertTrue(messages[-1][1])

    def test_unmarked_strips_seed_is_preserved_and_build_aborts(self):
        typed, attrs = self.strips_seed(marked=False)
        cmds, added, messages = self.run_build(typed, attrs)

        self.assertEqual([], added)
        self.assertTrue(cmds.objExists("seedWidth"))
        self.assertEqual(0.12, cmds.attrs["seedWidth"]["width"])
        self.assertIn("not managed by Groomist", messages[-1][0])

    def test_unmanaged_width_is_preserved_and_build_aborts(self):
        typed = [
            ("guidesGenerator", "HairFromGuidesNode"),
            ("userWidth", "ChangeWidthNode"),
        ]
        attrs = {"userWidth": {"width": 0.3}}
        cmds, added, messages = self.run_build(typed, attrs)

        self.assertEqual([], added)
        self.assertTrue(cmds.objExists("userWidth"))
        self.assertIn("not managed by Groomist", messages[-1][0])

    def test_multiple_widths_abort_without_mutation(self):
        typed, attrs = self.strips_seed(marked=True)
        typed.append(("otherWidth", "ChangeWidthNode"))
        attrs["otherWidth"] = {"width": 0.2}
        cmds, added, messages = self.run_build(typed, attrs)

        self.assertEqual([], added)
        self.assertTrue(cmds.objExists("seedWidth"))
        self.assertTrue(cmds.objExists("otherWidth"))
        self.assertIn("Multiple Change Width", messages[-1][0])

    def test_existing_build_operators_prevent_duplicate_build(self):
        typed, attrs = self.strips_seed(marked=True)
        typed.append(("surfaceComb1", "SurfaceCombNode"))
        cmds, added, messages = self.run_build(typed, attrs)

        self.assertEqual([], added)
        self.assertTrue(cmds.objExists("seedWidth"))
        self.assertIn("already contains Build Stack", messages[-1][0])

    def test_query_failure_uses_history_fallback(self):
        typed, attrs = self.strips_seed(marked=True)
        cmds, added, _ = self.run_build(typed, attrs, query_error=True)

        self.assertEqual(groomist.STACK_ORDER, added)
        self.assertTrue(any("Falling back" in warning for warning in cmds.warnings))

    def test_delete_failure_aborts_before_adding_operators(self):
        typed, attrs = self.strips_seed(marked=True)
        cmds, added, messages = self.run_build(
            typed,
            attrs,
            delete_failure=True,
        )

        self.assertEqual([], added)
        self.assertTrue(cmds.objExists("seedWidth"))
        self.assertIn("No new operators were added", messages[-1][0])

    def test_unreadable_width_aborts_before_deleting_managed_node(self):
        typed, attrs = self.strips_seed(marked=True)
        attrs["seedWidth"].pop("width")
        cmds, added, messages = self.run_build(typed, attrs)

        self.assertEqual([], added)
        self.assertTrue(cmds.objExists("seedWidth"))
        self.assertIn("Could not read", messages[-1][0])

    def test_add_failure_rolls_back_and_restores_width(self):
        typed, attrs = self.strips_seed(marked=True)
        cmds, added, messages = self.run_build(typed, attrs, fail_op="Curl")

        self.assertEqual(["SurfaceComb", "Rotate", "Clump", "ChangeWidth"], added)
        self.assertFalse(any(
            cmds.objExists(name)
            for name in ("SurfaceCombNew1", "RotateNew1", "ClumpNew1")
        ))
        self.assertTrue(cmds.objExists("ChangeWidthNew1"))
        self.assertEqual(0.12, cmds.attrs["ChangeWidthNew1"]["width"])
        self.assertIn("was restored", messages[-1][0])


if __name__ == "__main__":
    unittest.main()
