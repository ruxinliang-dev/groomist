"""
Groomist - Groom Builder for Autodesk Maya (Ornatrix)
=====================================================

A shelf tool that turns the C1 grooming pipeline into one-click setup and a
curated operator stack. Companion to Materialist.

Design principle (from the C1 article): silhouette -> regional control ->
render response -> handoff safety. The UI is ordered top-to-bottom to enforce
that decision order.

Sections:
    * Setup       - create the distribution (Fur Ball or Hair from Mesh Strips)
    * Build Stack - add the C1 operator stack in ONE click (heavy ops DISABLED),
                    ending with a ChangeWidth set to a low width
    * Performance - enable/disable operators on demand + drop viewport strands,
                    so a fully-built groom stays light in the viewport.
    * Rename      - rename the hair's outliner node with an _OX suffix.

Usage:
    import groomist
    groomist.show()

Compatibility: tested on Maya 2022 + Ornatrix for Maya 4.1.8; expected to work
on Maya 2022+ (Python 3) with Ornatrix 4.x. Version-specific node/command names
are grouped in the OX ADAPTER block below.
Author: Ruxin Liang  -  https://www.behance.net/ruxin-liang
License: MIT
"""

import maya.cmds as cmds
import maya.mel as mel

# ---------------------------------------------------------------------------
# Constants (UI shared with the Materialist palette so the tools match)
# ---------------------------------------------------------------------------

WINDOW_NAME = "groomistWindow"
WINDOW_TITLE = "Groomist - Groom Builder"

STATUS_OK = 0x2E7D32CC   # green
STATUS_ERR = 0xC62828CC  # red

COLOR_BG = (0.165, 0.157, 0.145)
COLOR_PANEL = (0.082, 0.075, 0.059)
COLOR_FIELD = (0.80, 0.53, 0.22)
BTN_PRIMARY = (0.78, 0.50, 0.17)
BTN_ACTION = (0.33, 0.31, 0.285)
BTN_DANGER = (0.45, 0.285, 0.105)

_OPTVAR_PREFIX = "groomist_collapse_"

_ui = {}

# ===========================================================================
# OX ADAPTER  --  Ornatrix-version-specific names, confirmed against 4.1.8.
# ===========================================================================

PLUGIN_NAME = "Ornatrix"

# Logical operator name -> Ornatrix node type (confirmed present in 4.1.8).
# NOTE: there is no LengthNode in 4.1.8, so "Length" is intentionally absent.
OP_TYPES = {
    "GroundStrands": "GroundStrandsNode",
    "ChangeWidth":   "ChangeWidthNode",
    "SurfaceComb":   "SurfaceCombNode",
    "Rotate":        "RotateNode",
    "Clump":         "ClumpNode",
    "Curl":          "CurlNode",
    "Frizz":         "FrizzNode",
    "Detail":        "DetailNode",
    "PushAway":      "PushAwayFromSurfaceNode",
    "Noise":         "NoiseNode",
    "Gravity":       "GravityNode",
}

# Node types that identify a "hair object" to operate on.
HAIR_SHAPE_TYPES = ("HairShape",)
GENERATOR_TYPES = ("HairFromGuidesNode", "HairFromMeshStripsNode")

# C1 operator order (section 3): direction -> shape -> variation. Built bottom
# (generator) upward in this order.
STACK_ORDER = ["SurfaceComb", "Rotate", "Clump", "Curl",
               "Frizz", "Detail", "Noise", "Gravity", "ChangeWidth"]

# Heavy operators -> built DISABLED so the viewport stays fast. The light,
# silhouette-defining ones stay enabled (C1: lock the big forms first).
HEAVY_OPS = {"Clump", "Curl", "Frizz", "Detail", "Noise"}

# View percentage for the viewport buttons. Applied as a 0-1 fraction on
# Hair-from-Guides (furball) and as a 0-100 value on Hair-from-Mesh-Strips.
VIEWPORT_LOW = 5
VIEWPORT_HIGH = 50

# Low default strand width set on the ChangeWidth operator that Build Stack adds.
CHANGE_WIDTH_DEFAULT = 0.05

# Low default guide length applied to a fresh furball (Ornatrix's own default is
# long). Change this if furballs come out too short/long for your model scale.
FURBALL_LENGTH_DEFAULT = 1.0


def _plugin_loaded():
    try:
        return bool(cmds.pluginInfo(PLUGIN_NAME, query=True, loaded=True))
    except Exception:
        return False


def _disable_branch_mode():
    """Prevent OxAddStrandOperator's Branch-Mode confirm dialog from blocking."""
    try:
        mel.eval("global int $OxBranchMode; $OxBranchMode = 0;")
    except Exception:
        pass


def _create_furball(mesh):
    """C1 section 2/4: grow strands from a scalp mesh. Returns the hair shape."""
    cmds.select(mesh, replace=True)
    hair = mel.eval("OxQuickHair")  # builds GuidesFromMesh->...->HairShape
    return hair or None


def _remove_render_settings(hair):
    """Delete the RenderSettings node OxQuickHair adds to a furball (width is
    handled by the ChangeWidth operator instead)."""
    try:
        stack = cmds.OxGetStackNodes(hair) or []
    except Exception:
        stack = []
    for n in stack:
        if cmds.nodeType(n) == "RenderSettingsNode":
            try:
                mel.eval('OxDeleteStrandOperator "{}"'.format(n))
            except Exception:
                try:
                    cmds.delete(n)
                except Exception as exc:
                    cmds.warning("Could not remove RenderSettings: {}".format(exc))


def _create_strips(mesh):
    """C1 section 3: build strands off hair-tube geometry. Returns the hair shape.

    Uses the menu MEL proc OxAddHairFromMeshStrips on the selected strip mesh
    (the raw OxHairFromMeshStrips command does not build the stack the way the
    menu does).
    """
    before = set(cmds.ls(type="HairShape", long=True) or [])
    cmds.select(mesh, replace=True)
    try:
        # The proc signature requires a string-array arg even though it then uses
        # the current selection; pass an empty array.
        result = mel.eval("string $oxStrips[]; OxAddHairFromMeshStrips($oxStrips)")
    except Exception as exc:
        cmds.warning("OxAddHairFromMeshStrips failed: {}".format(exc))
        result = None
    # The proc may not return the shape, so resolve it from the new scene state.
    new = [h for h in (cmds.ls(type="HairShape", long=True) or []) if h not in before]
    if new:
        return new[0]
    if isinstance(result, (list, tuple)):
        result = result[0] if result else None
    return result or _current_hair()


def _add_operator(op_logical, enabled=True):
    """Insert an Ornatrix operator into the selected hair's stack.

    Relies on OxAddStrandOperator(target, nodeType); target="" uses the current
    selection, and the proc selects the new operator, so sequential calls chain
    upward in call order. Returns the new node, or None.
    """
    node_type = OP_TYPES.get(op_logical)
    if not node_type:
        _msg("Unknown operator '{}'.".format(op_logical), ok=False)
        return None
    _disable_branch_mode()
    try:
        node = mel.eval('OxAddStrandOperator "" "{}"'.format(node_type))
    except Exception as exc:
        cmds.warning("OxAddStrandOperator failed for {}: {}".format(node_type, exc))
        return None
    if isinstance(node, (list, tuple)):
        node = node[0] if node else None
    if node:
        _set_op_enabled(node, enabled)
    return node


def _set_op_enabled(node, on):
    """Enable/disable an Ornatrix operator with OxEnableOperator (the same toggle
    as the stack-dialog checkbox); falls back to nodeState pass-through."""
    if not node or not cmds.objExists(node):
        return
    try:
        mel.eval('OxEnableOperator "{}" {}'.format(node, 1 if on else 0))
        return
    except Exception:
        pass
    try:
        cmds.setAttr(node + ".nodeState", 0 if on else 1)
    except Exception as exc:
        cmds.warning("Could not toggle {}: {}".format(node, exc))


def _set_view_percentage(percent):
    """Set the generator's view percentage to ``percent`` (e.g. 5 or 50).

    Handles both scales: Hair-from-Guides (furball) uses a 0-1 fraction
    (``viewportCountFraction``), while Hair-from-Mesh-Strips uses a 0-100
    "View Percentage". The cheapest global speedup, independent of which
    operators are on.
    """
    hair = _current_hair()
    gen = _generator_of(hair) if hair else None
    target = gen or hair
    if not target:
        _msg("Select a hair object first.", ok=False)
        return
    for attr in ("viewportCountFraction", "viewPercentage", "viewportFraction",
                 "displayFraction"):
        if not cmds.attributeQuery(attr, node=target, exists=True):
            continue
        # Decide the scale: a 0-100 percentage vs a 0-1 fraction.
        scale_100 = "percent" in attr.lower()
        if not scale_100:
            try:
                mx = cmds.attributeQuery(attr, node=target, maximum=True)
                scale_100 = bool(mx) and mx[0] > 1.5
            except Exception:
                scale_100 = False
        value = percent if scale_100 else percent / 100.0
        try:
            cmds.setAttr(target + "." + attr, value)
            _msg("View percentage set to {}%.".format(percent))
            return
        except Exception:
            pass
    _msg("No view-percentage attribute found on {}.".format(target), ok=False)


def _set_change_width(node, value):
    """Set the base width on a ChangeWidth operator. Returns the attr used, or None."""
    if not node:
        return None
    for attr in ("width", "value"):
        if cmds.attributeQuery(attr, node=node, exists=True):
            try:
                cmds.setAttr(node + "." + attr, value)
                return attr
            except Exception:
                pass
    for attr in (cmds.listAttr(node, settable=True) or []):
        al = attr.lower()
        if "width" in al and "ramp" not in al and "channel" not in al:
            try:
                cmds.setAttr(node + "." + attr, value)
                return attr
            except Exception:
                pass
    return None


def _set_guide_length(hair, value):
    """Set guide length on the furball's GuidesFromMesh node. Returns attr or None."""
    if not hair:
        return None
    try:
        stack = cmds.OxGetStackNodes(hair) or []
    except Exception:
        stack = cmds.listHistory(hair) or []
    gfm = next((n for n in stack if cmds.nodeType(n) == "GuidesFromMeshNode"), None)
    if not gfm:
        return None
    for attr in ("length", "guideLength"):
        if cmds.attributeQuery(attr, node=gfm, exists=True):
            try:
                cmds.setAttr(gfm + "." + attr, value)
                return attr
            except Exception:
                pass
    for attr in (cmds.listAttr(gfm, settable=True) or []):
        al = attr.lower()
        if "length" in al and al not in ("lengthrandomness", "lengthchannel",
                                         "islengthdependent"):
            try:
                cmds.setAttr(gfm + "." + attr, value)
                return attr
            except Exception:
                pass
    return None


# ===========================================================================
# Small helpers (Materialist-style)
# ===========================================================================

def _msg(text, ok=True):
    try:
        cmds.inViewMessage(statusMessage=text, fade=True, position="midCenterTop",
                           backColor=(STATUS_OK if ok else STATUS_ERR))
    except Exception:
        pass
    if not ok:
        cmds.warning(text)


def _repeatable(func):
    func()
    try:
        cmd = 'python("import {mod}; {mod}.{fn}()")'.format(mod=__name__, fn=func.__name__)
        cmds.repeatLast(addCommand=cmd, addCommandLabel=func.__name__)
    except Exception:
        pass


def _selected_mesh():
    """Return the first selected polygon mesh transform, or None."""
    for node in (cmds.ls(selection=True, long=True) or []):
        if cmds.listRelatives(node, shapes=True, type="mesh", fullPath=True):
            return node
    return None


def _current_hair():
    """Find the hair object to operate on, from selection, history, or cache."""
    for node in (cmds.ls(selection=True, long=True) or []):
        if cmds.nodeType(node) in HAIR_SHAPE_TYPES:
            return node
        for h in (cmds.listHistory(node) or []):
            if cmds.nodeType(h) in HAIR_SHAPE_TYPES:
                return h
    cached = _ui.get("last_hair")
    return cached if cached and cmds.objExists(cached) else None


def _generator_of(hair):
    """Return the HairFromGuides / HairFromMeshStrips node in the hair's stack."""
    try:
        stack = cmds.OxGetStackNodes(hair) or []
    except Exception:
        stack = cmds.listHistory(hair) or []
    for n in stack:
        if cmds.nodeType(n) in GENERATOR_TYPES:
            return n
    return None


def _source_meshes(hair):
    """Return the mesh SHAPE node(s) the groom is built from: the scalp (furball,
    via ``distributionMesh``) or the strip geometry (mesh-strips, via
    ``inputMesh``). Returning the shape rather than the transform lets us hide the
    geo without hiding a furball's hair, which is parented under the scalp."""
    gen = _generator_of(hair)
    shapes = set()
    for node, attr in ((gen, "distributionMesh"), (gen, "inputMesh"),
                       (hair, "distributionMesh")):
        if not node or not cmds.attributeQuery(attr, node=node, exists=True):
            continue
        for c in (cmds.listConnections(node + "." + attr, source=True,
                                       destination=False, shapes=True) or []):
            if cmds.nodeType(c) == "mesh":
                shapes.add(c)
    return list(shapes)


def _stack_operators(op_logical):
    node_type = OP_TYPES.get(op_logical)
    return cmds.ls(type=node_type) if node_type else []


# ===========================================================================
# High-level operations
# ===========================================================================

def setup_furball(*args):
    mesh = _selected_mesh()
    if not mesh:
        _msg("Select the Scalp_Geo mesh first.", ok=False)
        return
    if not _plugin_loaded():
        _msg("Ornatrix plugin is not loaded.", ok=False)
        return
    hair = _create_furball(mesh)
    if hair:
        _ui["last_hair"] = hair
        _remove_render_settings(hair)
        _set_guide_length(hair, FURBALL_LENGTH_DEFAULT)
        _msg("Fur Ball created on {} (length {}).".format(mesh, FURBALL_LENGTH_DEFAULT))
    else:
        _msg("Fur Ball creation failed.", ok=False)


def setup_strips(*args):
    mesh = _selected_mesh()
    if not mesh:
        _msg("Select the hair-strip geometry first.", ok=False)
        return
    if not _plugin_loaded():
        _msg("Ornatrix plugin is not loaded.", ok=False)
        return
    hair = _create_strips(mesh)
    if hair:
        _ui["last_hair"] = hair
        cmds.select(hair, replace=True)
        # C1: GroundStrands (attrs unchecked) + ChangeWidth as the base.
        _add_operator("GroundStrands", enabled=True)
        _add_operator("ChangeWidth", enabled=True)
        _msg("Hair-from-strips base created on {}.".format(mesh))
    else:
        _msg("Hair-from-strips creation failed.", ok=False)


def build_full_stack_disabled(*args):
    """Add the C1 operator stack in one click, heavy ops DISABLED.

    Light, silhouette-defining operators (SurfaceComb, Rotate, Gravity) stay
    enabled; heavy ones (Clump, Curl, Frizz, Detail, Noise) are added disabled
    so the viewport stays fast. Enable them on demand below.
    """
    hair = _current_hair()
    if not hair:
        _msg("Create or select a hair object first (use Setup).", ok=False)
        return
    cmds.select(hair, replace=True)
    try:
        existing_cw = next((n for n in (cmds.OxGetStackNodes(hair) or [])
                            if cmds.nodeType(n) == "ChangeWidthNode"), None)
    except Exception:
        existing_cw = None
    built, disabled = [], []
    for op in STACK_ORDER:
        # Strips already add a ChangeWidth; don't add a second - just set it low.
        if op == "ChangeWidth" and existing_cw:
            _set_change_width(existing_cw, CHANGE_WIDTH_DEFAULT)
            continue
        on = op not in HEAVY_OPS
        node = _add_operator(op, enabled=on)
        if node:
            built.append(node)
            if op == "ChangeWidth":
                _set_change_width(node, CHANGE_WIDTH_DEFAULT)
            if not on:
                disabled.append(op)
    if not built:
        _msg("No operators were added.", ok=False)
        return
    cmds.select(hair, replace=True)
    _msg("Built {} operators. Disabled for speed: {}.".format(
        len(built), ", ".join(disabled) if disabled else "none"))


def enable_heavy(*args):
    _toggle_heavy(True)


def disable_heavy(*args):
    _toggle_heavy(False)


def _toggle_heavy(on):
    touched = 0
    for op in HEAVY_OPS:
        for node in _stack_operators(op):
            _set_op_enabled(node, on)
            touched += 1
    _msg("{} {} heavy operator(s).".format("Enabled" if on else "Disabled", touched))


def viewport_low(*args):
    _set_view_percentage(VIEWPORT_LOW)


def viewport_high(*args):
    _set_view_percentage(VIEWPORT_HIGH)


def toggle_source_mesh(*args):
    """Show/hide the original mesh the selected groom is built from.

    Toggles the mesh SHAPE's visibility (not its transform), so a furball's hair
    — parented under the scalp transform — stays visible when the scalp is hidden.
    """
    hair = _current_hair()
    if not hair:
        _msg("Select a groom first.", ok=False)
        return
    shapes = _source_meshes(hair)
    if not shapes:
        _msg("Couldn't find the source mesh for this groom.", ok=False)
        return
    new_vis = not cmds.getAttr(shapes[0] + ".visibility")
    for s in shapes:
        try:
            cmds.setAttr(s + ".visibility", new_vis)
        except Exception:
            pass
    _msg("{} the source mesh.".format("Showed" if new_vis else "Hid"))


def recreate_clumps(*args):
    """Delete then recreate clumps so they rebuild from the current groom (C1 tip
    for clumps that go buggy after an upstream change). Mirrors the Clump
    operator's Delete + Create Clump(s) buttons, and leaves the Clump node(s)
    selected afterwards (OxEditClumps otherwise selects the distribution mesh)."""
    sel = cmds.ls(selection=True, long=True) or []
    clumps = [n for n in sel if cmds.nodeType(n) == "ClumpNode"]
    if not clumps:
        hair = _current_hair()
        stack = []
        if hair:
            try:
                stack = cmds.OxGetStackNodes(hair) or []
            except Exception:
                stack = []
        clumps = [n for n in stack if cmds.nodeType(n) == "ClumpNode"]
    if not clumps:
        _msg("Select a Clump operator (or a groom that has one).", ok=False)
        return
    cmds.select(clear=True)  # empty selection -> OxEditClumps acts on ALL clumps
    done = 0
    for c in clumps:
        try:
            method = cmds.getAttr(c + ".clumpCreateMethod")
            count = cmds.getAttr(c + ".clumpCount")
            seed = cmds.getAttr(c + ".randomSeed")
            mel.eval('OxEditClumps "{}" -d'.format(c))
            mel.eval('OxEditClumps "{}" -c {} {} {}'.format(c, method, count, seed))
            done += 1
        except Exception as exc:
            cmds.warning("Recreate clumps failed on {}: {}".format(c, exc))
    # Restore selection to the Clump node(s) so editing can continue on them.
    existing = [c for c in clumps if cmds.objExists(c)]
    if existing:
        cmds.select(existing, replace=True)
    _msg("Recreated clumps on {} operator(s).".format(done))


def rename_hair(*args):
    """Rename the current Ornatrix hair's outliner node, appending an _OX suffix."""
    hair = _current_hair()
    if not hair:
        _msg("Select a groom first.", ok=False)
        return
    base = (cmds.textField(_ui["ox_name"], query=True, text=True) or "").strip()
    if not base:
        _msg("Type a name first.", ok=False)
        return
    name = base if base.endswith("_OX") else base + "_OX"
    parent = cmds.listRelatives(hair, parent=True, fullPath=True)
    target = parent[0] if parent else hair
    try:
        new = cmds.rename(target, name)
    except Exception as exc:
        _msg("Rename failed: {}".format(exc), ok=False)
        return
    shapes = cmds.listRelatives(new, shapes=True, fullPath=True, type="HairShape") or []
    if shapes:
        _ui["last_hair"] = shapes[0]
    _msg("Renamed to {}.".format(new))


# ===========================================================================
# UI
# ===========================================================================

def _saved_collapse(key, default):
    var = _OPTVAR_PREFIX + key
    if cmds.optionVar(exists=var):
        return bool(cmds.optionVar(query=var))
    return default


def _save_collapse(key, collapsed):
    cmds.optionVar(intValue=(_OPTVAR_PREFIX + key, int(collapsed)))


def show():
    if cmds.window(WINDOW_NAME, exists=True):
        cmds.deleteUI(WINDOW_NAME, window=True)

    window = cmds.window(WINDOW_NAME, title=WINDOW_TITLE, sizeable=True,
                         backgroundColor=COLOR_BG, widthHeight=(340, 560))
    cmds.scrollLayout(childResizable=True, backgroundColor=COLOR_BG)
    root = cmds.columnLayout(adjustableColumn=True, rowSpacing=6, backgroundColor=COLOR_BG)

    def _section(label, key, collapse_default=False):
        cmds.setParent(root)
        cmds.frameLayout(label=label, collapsable=True,
                         collapse=_saved_collapse(key, collapse_default),
                         collapseCommand=lambda k=key: _save_collapse(k, True),
                         expandCommand=lambda k=key: _save_collapse(k, False),
                         marginWidth=4, marginHeight=6, backgroundColor=COLOR_BG)
        cmds.columnLayout(adjustableColumn=True, rowSpacing=6, backgroundColor=COLOR_BG)

    # --- Setup ------------------------------------------------------------
    _section("1 - Setup", "setup")
    cmds.text(label="Select the scalp / strip mesh, then:", align="left", height=20)
    cmds.button(label="Fur Ball (from scalp)", backgroundColor=BTN_PRIMARY,
                command=lambda *_: _repeatable(setup_furball),
                annotation="Grow strands from a scalp mesh - short fur, brows, animal fur")
    cmds.button(label="Hair from Mesh Strips", backgroundColor=BTN_PRIMARY,
                command=lambda *_: _repeatable(setup_strips),
                annotation="Build strands off hair-tube geometry - long, graphic hair")
    cmds.button(label="Show / Hide Source Mesh", backgroundColor=BTN_ACTION,
                command=lambda *_: _repeatable(toggle_source_mesh),
                annotation="Toggle visibility of the original scalp/strip mesh the groom is built from")

    # --- Build Stack ------------------------------------------------------
    _section("2 - Build Stack", "stack")
    cmds.text(label="Adds the C1 stack at once,\nheavy operators built disabled.",
              align="left", height=34)
    cmds.button(label="Build Full Stack (disabled)", backgroundColor=BTN_PRIMARY,
                command=lambda *_: _repeatable(build_full_stack_disabled),
                annotation="SurfaceComb-Rotate-Clump-Curl-Frizz-Detail-Noise-Gravity-ChangeWidth; "
                           "heavy ops added disabled; ChangeWidth set to a low width")

    # --- Performance ------------------------------------------------------
    _section("3 - Performance", "performance")
    cmds.text(label="Keep a fully-built groom light:", align="left", height=20)
    cmds.button(label="Enable Heavy Operators", backgroundColor=BTN_ACTION,
                command=lambda *_: _repeatable(enable_heavy),
                annotation="Turn the heavy operators back on for review/render")
    cmds.button(label="Disable Heavy Operators", backgroundColor=BTN_ACTION,
                command=lambda *_: _repeatable(disable_heavy),
                annotation="Switch Clump/Curl/Frizz/Detail/Noise off - the fast state")
    cmds.separator(height=6, style="none")
    cmds.button(label="Viewport Strands: 5%", backgroundColor=BTN_ACTION,
                command=lambda *_: _repeatable(viewport_low),
                annotation="Set view percentage to 5% - cheapest global speedup")
    cmds.button(label="Viewport Strands: 50%", backgroundColor=BTN_ACTION,
                command=lambda *_: _repeatable(viewport_high),
                annotation="Set view percentage to 50%")

    # --- Clumps -----------------------------------------------------------
    _section("4 - Clumps", "clumps")
    cmds.button(label="Recreate Clumps", backgroundColor=BTN_ACTION,
                command=lambda *_: _repeatable(recreate_clumps),
                annotation="Delete then recreate clumps - rebuilds them from the current groom")

    # --- Rename -----------------------------------------------------------
    _section("5 - Rename", "rename")
    cmds.text(label="Rename hair in outliner (adds _OX):", align="left", height=20)
    _ui["ox_name"] = cmds.textField(text="", backgroundColor=COLOR_FIELD,
                                    placeholderText="e.g. catFur",
                                    annotation="New name for the hair; _OX is appended automatically")
    cmds.button(label="Apply Name", backgroundColor=BTN_ACTION,
                command=lambda *_: _repeatable(rename_hair),
                annotation="Rename the current hair's outliner node to <name>_OX")

    cmds.showWindow(window)


if __name__ == "__main__":
    show()
