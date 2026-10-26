bl_info = {
    "name": "Subframe Scrubber & Keyframe Snap",
    "author": "Antigravity",
    "version": (1, 0, 0),
    "blender": (5, 0, 0),
    "location": "3D Viewport, Timeline, Dope Sheet, Graph Editor",
    "description": "Scrub time with subframe preview via Ctrl + RMB drag and snap inserted keyframes to whole frames.",
    "category": "Animation",
}

import bpy
import blf
import math


def get_all_fcurves(action):
    """Retrieve all fcurves from both Blender 5.x layered/slotted actions and legacy actions."""
    if not action:
        return []
    if hasattr(action, "layers"):
        curves = []
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    curves.extend(bag.fcurves)
        return curves
    elif hasattr(action, "fcurves"):
        return list(action.fcurves)
    return []


def snap_action_fcurves_to_integer(action, target_frame=None, tolerance=0.5):
    """Snaps keyframe points in an action to integer frames."""
    fcurves = get_all_fcurves(action)
    total_snapped = 0
    for fc in fcurves:
        modified = False
        for kp in fc.keyframe_points:
            diff = abs(kp.co.x - round(kp.co.x))
            if diff > 1e-4:
                if target_frame is not None and abs(kp.co.x - target_frame) > tolerance:
                    continue
                dx = round(kp.co.x) - kp.co.x
                kp.handle_left.x += dx
                kp.handle_right.x += dx
                kp.co.x += dx
                modified = True
                total_snapped += 1
        if modified:
            fc.update()
    return total_snapped


def get_target_frame(frame_float, mode='ROUND'):
    if mode == 'FLOOR':
        return math.floor(frame_float)
    elif mode == 'CEIL':
        return math.ceil(frame_float)
    return round(frame_float)


def stop_all_playback(context):
    """Halts any running playback (both slowmotion_playback and native animation) before scrubbing."""
    scene = context.scene
    # 1. Stop slow-motion playback addon if running
    if getattr(scene, "slowmo_is_playing", False):
        scene.slowmo_is_playing = False
        try:
            import sys
            if "slowmotion_playback" in sys.modules:
                sys.modules["slowmotion_playback"]._playback_state["is_running"] = False
        except Exception:
            pass

    # 2. Stop standard Blender animation playback if running
    if context.screen and getattr(context.screen, "is_animation_playing", False):
        try:
            bpy.ops.screen.animation_cancel()
        except Exception:
            try:
                bpy.ops.screen.animation_play()
            except Exception:
                pass


class SubframeScrubberPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    sensitivity: bpy.props.FloatProperty(
        name="Scrub Sensitivity",
        description="Amount of frames changed per pixel dragged",
        default=0.05,
        min=0.001,
        max=1.0,
    )
    show_hud: bpy.props.BoolProperty(
        name="Show Viewport HUD",
        description="Draw real-time floating frame text in 3D Viewport while scrubbing",
        default=True,
    )
    snap_mode: bpy.props.EnumProperty(
        name="Snap Mode",
        description="Rule for snapping subframe playhead to integer frames",
        items=[
            ('ROUND', "Nearest Frame", "Snap to the nearest full frame (e.g. 10.4 -> 10, 10.6 -> 11)"),
            ('FLOOR', "Previous Frame", "Always snap down to previous whole frame"),
            ('CEIL', "Next Frame", "Always snap up to next whole frame"),
        ],
        default='ROUND',
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "sensitivity")
        layout.prop(self, "show_hud")
        layout.prop(self, "snap_mode")


class ANIM_OT_subframe_scrub(bpy.types.Operator):
    """Scrub timeline with continuous subframe preview (Ctrl + RMB Drag)"""
    bl_idname = "anim.subframe_scrub"
    bl_label = "Subframe Scrub"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    init_mouse_x: int = 0
    init_mouse_y: int = 0
    init_frame_float: float = 0.0
    current_frame: float = 0.0
    draw_handle = None

    def draw_hud(self, context):
        prefs = context.preferences.addons.get(__name__, None)
        if prefs and not prefs.preferences.show_hud:
            return

        font_id = 0
        blf.size(font_id, 16)
        blf.color(font_id, 0.95, 0.95, 0.95, 0.95)

        nearest = round(self.current_frame)
        text = f"Subframe Scrub: {self.current_frame:.2f}  |  Nearest: {nearest}  [Shift: fine | Alt: fast]"
        
        # Draw text at top-left of the viewport with comfortable padding
        blf.position(font_id, 30, 40, 0)
        blf.draw(font_id, text)

    def remove_draw_handler(self):
        if self.draw_handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self.draw_handle, 'WINDOW')
            self.draw_handle = None

    def invoke(self, context, event):
        # Stop any active playback (slow-motion or native) so scrubbing takes immediate control
        stop_all_playback(context)

        scene = context.scene
        scene.show_subframe = True

        self.init_mouse_x = event.mouse_x
        self.init_mouse_y = event.mouse_y
        self.init_frame_float = scene.frame_float
        self.current_frame = scene.frame_float

        # Attach viewport HUD if in 3D View
        if context.area and context.area.type == 'VIEW_3D':
            self.draw_handle = bpy.types.SpaceView3D.draw_handler_add(
                self.draw_hud, (context,), 'WINDOW', 'POST_PIXEL'
            )

        context.workspace.status_text_set(
            f"Subframe Scrub: {self.current_frame:.2f} (Nearest: {round(self.current_frame)}) | "
            "RMB Release: Confirm | ESC: Cancel"
        )

        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        scene = context.scene
        prefs = context.preferences.addons.get(__name__, None)
        base_sensitivity = prefs.preferences.sensitivity if prefs else 0.05

        if event.type == 'MOUSEMOVE':
            dx = event.mouse_x - self.init_mouse_x
            mult = 0.2 if event.shift else (5.0 if event.alt else 1.0)
            target = self.init_frame_float + (dx * base_sensitivity * mult)

            self.current_frame = target
            int_frame = int(math.floor(target))
            subframe = target - int_frame
            scene.frame_set(int_frame, subframe=subframe)

            context.workspace.status_text_set(
                f"Subframe Scrub: {target:.2f} (Nearest: {round(target)}) | "
                "Shift: Fine | Alt: Fast | Release RMB to confirm"
            )

            if context.area:
                context.area.tag_redraw()

            return {'RUNNING_MODAL'}

        elif event.type == 'RIGHTMOUSE' and event.value == 'RELEASE':
            self.remove_draw_handler()
            context.workspace.status_text_set(None)
            if context.area:
                context.area.tag_redraw()
            return {'FINISHED'}

        elif event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            self.remove_draw_handler()
            context.workspace.status_text_set(None)
            if context.area:
                context.area.tag_redraw()
            return {'FINISHED'}

        elif event.type in {'ESC'}:
            self.remove_draw_handler()
            context.workspace.status_text_set(None)
            int_frame = int(math.floor(self.init_frame_float))
            subframe = self.init_frame_float - int_frame
            scene.frame_set(int_frame, subframe=subframe)
            if context.area:
                context.area.tag_redraw()
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}


class ANIM_OT_snap_keyframe_insert_menu(bpy.types.Operator):
    """Insert Keyframe and automatically snap to integer frame"""
    bl_idname = "anim.snap_keyframe_insert_menu"
    bl_label = "Insert Keyframe (Snapped)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        prefs = context.preferences.addons.get(__name__, None)
        snap_mode = prefs.preferences.snap_mode if prefs else 'ROUND'

        # If playhead is on a subframe, snap time to integer frame first without losing unkeyed changes
        if abs(scene.frame_subframe) > 1e-4:
            target = get_target_frame(scene.frame_float, snap_mode)
            scene.frame_current = int(target)
            scene.frame_subframe = 0.0

        # Invoke the native keyframe insert menu
        res = bpy.ops.anim.keyframe_insert_menu('INVOKE_DEFAULT')

        # Safety post-check: if any subframe keyframes were placed, snap them
        for obj in context.selected_objects or ([context.active_object] if context.active_object else []):
            if obj and obj.animation_data and obj.animation_data.action:
                snap_action_fcurves_to_integer(obj.animation_data.action, target_frame=scene.frame_current)

        return res

    def invoke(self, context, event):
        return self.execute(context)


class ANIM_OT_snap_keyframe_insert(bpy.types.Operator):
    """Insert Keyframe for active Keying Set and snap to integer frame"""
    bl_idname = "anim.snap_keyframe_insert"
    bl_label = "Insert Keyframe Snapped (Keying Set)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        prefs = context.preferences.addons.get(__name__, None)
        snap_mode = prefs.preferences.snap_mode if prefs else 'ROUND'

        if abs(scene.frame_subframe) > 1e-4:
            target = get_target_frame(scene.frame_float, snap_mode)
            scene.frame_current = int(target)
            scene.frame_subframe = 0.0

        res = bpy.ops.anim.keyframe_insert()

        for obj in context.selected_objects or ([context.active_object] if context.active_object else []):
            if obj and obj.animation_data and obj.animation_data.action:
                snap_action_fcurves_to_integer(obj.animation_data.action, target_frame=scene.frame_current)

        return res


class ANIM_OT_snap_action_subframes(bpy.types.Operator):
    """Snap all existing subframe keyframes in selected objects to whole frames"""
    bl_idname = "anim.snap_action_subframes"
    bl_label = "Snap Subframe Keys to Whole Frames"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        total = 0
        objects = context.selected_objects or ([context.active_object] if context.active_object else [])
        for obj in objects:
            if obj.animation_data and obj.animation_data.action:
                total += snap_action_fcurves_to_integer(obj.animation_data.action)

        self.report({'INFO'}, f"Snapped {total} subframe keyframe point(s) to whole frames.")
        return {'FINISHED'}


# Topbar / Timeline menu helper
def draw_timeline_header(self, context):
    layout = self.layout
    layout.separator()
    layout.operator("anim.snap_action_subframes", text="", icon='SNAP_ON')


classes = (
    SubframeScrubberPreferences,
    ANIM_OT_subframe_scrub,
    ANIM_OT_snap_keyframe_insert_menu,
    ANIM_OT_snap_keyframe_insert,
    ANIM_OT_snap_action_subframes,
)

addon_keymaps = []

KM_SCRUB_CONFIGS = [
    ("3D View", "VIEW_3D", "WINDOW"),
    ("Dopesheet", "DOPESHEET_EDITOR", "WINDOW"),
    ("Graph Editor", "GRAPH_EDITOR", "WINDOW"),
]

KM_INSERT_CONFIGS = [
    ("3D View", "VIEW_3D", "WINDOW"),
    ("Dopesheet", "DOPESHEET_EDITOR", "WINDOW"),
    ("Pose", "EMPTY", "WINDOW"),
    ("Object Mode", "EMPTY", "WINDOW"),
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.DOPESHEET_HT_header.append(draw_timeline_header)

    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return

    unregister_keymaps()

    # Register Ctrl + RMB Drag scrubber
    for km_name, space_type, region_type in KM_SCRUB_CONFIGS:
        km = kc.keymaps.new(name=km_name, space_type=space_type, region_type=region_type)
        kmi = km.keymap_items.new("anim.subframe_scrub", type="RIGHTMOUSE", value="PRESS", ctrl=True)
        addon_keymaps.append((km, kmi))

    # Register Snapped Keyframe Insert (Hotkey I)
    for km_name, space_type, region_type in KM_INSERT_CONFIGS:
        km = kc.keymaps.new(name=km_name, space_type=space_type, region_type=region_type)
        kmi_menu = km.keymap_items.new("anim.snap_keyframe_insert_menu", type="I", value="PRESS")
        addon_keymaps.append((km, kmi_menu))
        # Snapped Keyingset Insert (Hotkey K)
        kmi_ks = km.keymap_items.new("anim.snap_keyframe_insert", type="K", value="PRESS")
        addon_keymaps.append((km, kmi_ks))


def unregister_keymaps():
    for km, kmi in addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    addon_keymaps.clear()


def unregister():
    bpy.types.DOPESHEET_HT_header.remove(draw_timeline_header)
    unregister_keymaps()

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
