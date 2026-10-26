bl_info = {
    "name": "Viewport Slow-Motion Playback",
    "author": "Antigravity",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "Timeline Header, 3D Viewport Header, Keymaps (Shift+Alt+Space, Alt+Wheel)",
    "description": "Smooth slow-motion playback with subframe interpolation and Alt+Wheel speed control",
    "category": "Animation",
}

import bpy
import time
import math

# Ordered speed presets for Alt + Wheel stepping
SPEED_PRESETS = [0.05, 0.1, 0.15, 0.2, 0.25, 0.33, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0]

# Internal runtime playback state (not saved in .blend)
_playback_state = {
    "is_running": False,
    "start_real_time": 0.0,
    "start_frame": 0.0,
    "current_subframe_pos": 0.0,
    "status_timer_active": False,
}

addon_keymaps = []


def get_closest_preset_index(current_speed):
    """Find index of the preset closest to the current speed value."""
    closest_idx = 0
    min_diff = float("inf")
    for i, p in enumerate(SPEED_PRESETS):
        diff = abs(p - current_speed)
        if diff < min_diff:
            min_diff = diff
            closest_idx = i
    return closest_idx


def update_speed_state(self, context):
    """Called whenever slowmo_speed property changes to keep playback continuous without jumps."""
    if _playback_state["is_running"]:
        now = time.perf_counter()
        _playback_state["start_frame"] = _playback_state["current_subframe_pos"]
        _playback_state["start_real_time"] = now


def slowmo_timer_tick():
    """Timer callback running at display refresh rate (~60Hz)."""
    if not _playback_state["is_running"]:
        return None

    scene = bpy.context.scene
    if not scene:
        _playback_state["is_running"] = False
        return None

    if not getattr(scene, "slowmo_is_playing", False):
        _playback_state["is_running"] = False
        return None

    now = time.perf_counter()
    elapsed = now - _playback_state["start_real_time"]
    
    fps = scene.render.fps / scene.render.fps_base if scene.render.fps_base > 0 else scene.render.fps
    speed = max(0.01, scene.slowmo_speed)

    target_frame = _playback_state["start_frame"] + (elapsed * fps * speed)
    frame_start = float(scene.frame_start)
    frame_end = float(scene.frame_end)
    frame_range = max(1.0, frame_end - frame_start)

    # Handle forward looping
    if target_frame > frame_end:
        offset = (target_frame - frame_start) % frame_range
        target_frame = frame_start + offset
        _playback_state["start_real_time"] = now - (offset / (fps * speed))
        _playback_state["start_frame"] = frame_start
    elif target_frame < frame_start:
        target_frame = frame_start
        _playback_state["start_real_time"] = now
        _playback_state["start_frame"] = frame_start

    _playback_state["current_subframe_pos"] = target_frame

    int_frame = int(math.floor(target_frame))
    subframe = (target_frame - int_frame) if scene.slowmo_subframe_interpolation else 0.0

    # Evaluate dependency graph with subframe accuracy
    scene.frame_set(int_frame, subframe=subframe)

    # Request redraw of animation and 3D Viewport editors
    for window in bpy.context.window_manager.windows:
        screen = window.screen
        if screen:
            for area in screen.areas:
                if area.type in {'VIEW_3D', 'DOPESHEET_EDITOR', 'TIMELINE', 'GRAPH_EDITOR'}:
                    area.tag_redraw()

    return 1.0 / 60.0


def clear_status_text():
    """Clears the workspace status bar text after timeout."""
    try:
        ws = bpy.context.workspace
        if ws:
            ws.status_text_set(None)
    except Exception:
        pass
    _playback_state["status_timer_active"] = False
    return None


class SCREEN_OT_slowmo_toggle(bpy.types.Operator):
    """Toggle slow-motion playback with subframe interpolation"""
    bl_idname = "screen.slowmo_toggle"
    bl_label = "Toggle Slow-Mo Playback"
    bl_options = {"REGISTER"}

    def execute(self, context):
        scene = context.scene

        if scene.slowmo_is_playing:
            # Stop playback
            scene.slowmo_is_playing = False
            _playback_state["is_running"] = False
            # Cleanly set frame to nearest integer frame
            curr = scene.frame_current_final if hasattr(scene, "frame_current_final") else scene.frame_current
            scene.frame_set(int(round(curr)), subframe=0.0)
            self.report({'INFO'}, "Slow-Mo Playback Stopped")
        else:
            # If native Blender animation is playing, pause it first
            if context.screen and context.screen.is_animation_playing:
                bpy.ops.screen.animation_play()

            scene.slowmo_is_playing = True
            _playback_state["is_running"] = True
            _playback_state["start_real_time"] = time.perf_counter()
            _playback_state["start_frame"] = float(scene.frame_current_final if hasattr(scene, "frame_current_final") else scene.frame_current)
            _playback_state["current_subframe_pos"] = _playback_state["start_frame"]

            # Register timer if not already registered
            if not bpy.app.timers.is_registered(slowmo_timer_tick):
                bpy.app.timers.register(slowmo_timer_tick, first_interval=0.0, persistent=True)

            self.report({'INFO'}, f"Slow-Mo Playback: {scene.slowmo_speed:.2f}x")

        # Tag redraw for headers
        for area in context.screen.areas:
            area.tag_redraw()

        return {'FINISHED'}


class SCREEN_OT_slowmo_change_speed(bpy.types.Operator):
    """Change slow-motion playback speed multiplier (Alt + Wheel Up/Down)"""
    bl_idname = "screen.slowmo_change_speed"
    bl_label = "Change Slow-Mo Speed"
    bl_options = {"REGISTER"}

    direction: bpy.props.EnumProperty(
        name="Direction",
        items=[
            ('UP', "Faster", "Increase playback speed"),
            ('DOWN', "Slower", "Decrease playback speed"),
        ],
        default='UP'
    )

    def execute(self, context):
        scene = context.scene
        current_idx = get_closest_preset_index(scene.slowmo_speed)

        if self.direction == 'UP':
            new_idx = min(len(SPEED_PRESETS) - 1, current_idx + 1)
        else:
            new_idx = max(0, current_idx - 1)

        new_speed = SPEED_PRESETS[new_idx]
        scene.slowmo_speed = new_speed

        speed_text = f"Slow-Mo Speed: {new_speed:.2f}x"
        self.report({'INFO'}, speed_text)

        # Show in workspace status bar
        try:
            ws = context.workspace
            if ws:
                ws.status_text_set(speed_text)
                if not _playback_state["status_timer_active"]:
                    _playback_state["status_timer_active"] = True
                    bpy.app.timers.register(clear_status_text, first_interval=1.5)
        except Exception:
            pass

        # Redraw headers to reflect change immediately
        if context.screen:
            for area in context.screen.areas:
                area.tag_redraw()

        return {'FINISHED'}


class SCREEN_OT_slowmo_set_preset(bpy.types.Operator):
    """Set a specific slow-motion speed preset"""
    bl_idname = "screen.slowmo_set_preset"
    bl_label = "Set Slow-Mo Speed"
    bl_options = {"REGISTER"}

    speed: bpy.props.FloatProperty(name="Speed", default=0.5)

    def execute(self, context):
        context.scene.slowmo_speed = self.speed
        self.report({'INFO'}, f"Slow-Mo Speed: {self.speed:g}x")
        return {'FINISHED'}


class SLOWMO_MT_speed_presets(bpy.types.Menu):
    """Dropdown menu for selecting playback speed presets"""
    bl_idname = "SLOWMO_MT_speed_presets"
    bl_label = "Playback Speed"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        curr = scene.slowmo_speed

        layout.label(text="Slow Motion:", icon='TIME')
        for p in [0.05, 0.1, 0.15, 0.2, 0.25, 0.33, 0.5, 0.75]:
            is_active = (abs(curr - p) < 0.015)
            op = layout.operator(
                "screen.slowmo_set_preset",
                text=f"{p:g}x  ({int(p * 100)}%)",
                icon='CHECKMARK' if is_active else 'BLANK1'
            )
            op.speed = p

        layout.separator()
        layout.label(text="Normal & Fast:", icon='FORWARD')
        for p in [1.0, 1.25, 1.5, 2.0]:
            is_active = (abs(curr - p) < 0.015)
            label = "1.0x (Normal)" if p == 1.0 else f"{p:g}x  ({int(p * 100)}%)"
            op = layout.operator(
                "screen.slowmo_set_preset",
                text=label,
                icon='CHECKMARK' if is_active else 'BLANK1'
            )
            op.speed = p

        layout.separator()
        layout.prop(scene, "slowmo_subframe_interpolation", text="Interpolate Subframes")


def draw_timeline_header(self, context):
    """Draw slow-mo controls in the Timeline/DopeSheet header."""
    scene = context.scene
    if not hasattr(scene, "slowmo_speed"):
        return

    layout = self.layout
    row = layout.row(align=True)

    # Play/Stop toggle button
    is_playing = scene.slowmo_is_playing
    icon = 'PAUSE' if is_playing else 'PLAY'
    row.operator("screen.slowmo_toggle", text="SlowMo", icon=icon, depress=is_playing)

    # Fine-tuning slider (fixed width in header, displays current speed inside)
    row.prop(scene, "slowmo_speed", text="", slider=True)

    # Dropdown menu button with fixed arrow icon (no text to prevent width jitter on the bar)
    row.menu("SLOWMO_MT_speed_presets", text="", icon='DOWNARROW_HLT')


@bpy.app.handlers.persistent
def on_load_pre(dummy):
    """Stop slow-motion timer safely before loading a new blend file."""
    _playback_state["is_running"] = False
    if bpy.app.timers.is_registered(slowmo_timer_tick):
        bpy.app.timers.unregister(slowmo_timer_tick)


classes = (
    SCREEN_OT_slowmo_toggle,
    SCREEN_OT_slowmo_change_speed,
    SCREEN_OT_slowmo_set_preset,
    SLOWMO_MT_speed_presets,
)


def register_keymaps():
    """Register Shift+Alt+Space and Alt+Wheel keymaps in 3D View and DopeSheet."""
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return

    # View 3D Keymaps
    km_v3d = kc.keymaps.new(name="3D View", space_type="VIEW_3D")
    
    kmi = km_v3d.keymap_items.new("screen.slowmo_toggle", type='SPACE', value='PRESS', shift=True, alt=True)
    addon_keymaps.append((km_v3d, kmi))

    kmi = km_v3d.keymap_items.new("screen.slowmo_change_speed", type='WHEELUPMOUSE', value='PRESS', alt=True)
    kmi.properties.direction = 'UP'
    addon_keymaps.append((km_v3d, kmi))

    kmi = km_v3d.keymap_items.new("screen.slowmo_change_speed", type='WHEELDOWNMOUSE', value='PRESS', alt=True)
    kmi.properties.direction = 'DOWN'
    addon_keymaps.append((km_v3d, kmi))

    # Dope Sheet / Timeline Keymaps
    km_ds = kc.keymaps.new(name="Dopesheet", space_type="DOPESHEET_EDITOR")
    
    kmi = km_ds.keymap_items.new("screen.slowmo_toggle", type='SPACE', value='PRESS', shift=True, alt=True)
    addon_keymaps.append((km_ds, kmi))

    kmi = km_ds.keymap_items.new("screen.slowmo_change_speed", type='WHEELUPMOUSE', value='PRESS', alt=True)
    kmi.properties.direction = 'UP'
    addon_keymaps.append((km_ds, kmi))

    kmi = km_ds.keymap_items.new("screen.slowmo_change_speed", type='WHEELDOWNMOUSE', value='PRESS', alt=True)
    kmi.properties.direction = 'DOWN'
    addon_keymaps.append((km_ds, kmi))


def unregister_keymaps():
    """Unregister all addon keymap items."""
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Scene properties
    bpy.types.Scene.slowmo_speed = bpy.props.FloatProperty(
        name="Speed",
        description="Slow-motion playback speed multiplier",
        default=0.5,
        min=0.01,
        max=3.0,
        precision=2,
        step=5,
        update=update_speed_state
    )

    bpy.types.Scene.slowmo_is_playing = bpy.props.BoolProperty(
        name="Slow-Mo Playing",
        description="Whether slow-motion playback is active",
        default=False
    )

    bpy.types.Scene.slowmo_subframe_interpolation = bpy.props.BoolProperty(
        name="Subframe Interpolation",
        description="Smoothly evaluate subframes during playback",
        default=True
    )

    # UI Header Integrations
    bpy.types.DOPESHEET_HT_header.append(draw_timeline_header)

    # Handlers & Keymaps
    bpy.app.handlers.load_pre.append(on_load_pre)
    register_keymaps()


def unregister():
    # Stop timer if active
    _playback_state["is_running"] = False
    if bpy.app.timers.is_registered(slowmo_timer_tick):
        bpy.app.timers.unregister(slowmo_timer_tick)

    # Keymaps & Handlers
    unregister_keymaps()
    if on_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(on_load_pre)

    # UI Headers
    bpy.types.DOPESHEET_HT_header.remove(draw_timeline_header)

    # Properties
    del bpy.types.Scene.slowmo_speed
    del bpy.types.Scene.slowmo_is_playing
    del bpy.types.Scene.slowmo_subframe_interpolation

    # Classes
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
