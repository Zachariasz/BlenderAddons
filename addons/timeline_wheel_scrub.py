bl_info = {
    "name": "Timeline Wheel Scrub",
    "author": "Antigravity & Qwen",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "3D Viewport, Timeline, Dope Sheet, Graph Editor",
    "description": "Scrub the timeline forward/backward using Ctrl + Mouse Wheel in 3D View, Timeline, Dope Sheet, and Graph Editor.",
    "category": "Animation",
}

import bpy

addon_keymaps = []

KM_DEFINITIONS = [
    # (Keymap Name, Space Type, Region Type)
    ("3D View", "VIEW_3D", "WINDOW"),
    ("Dopesheet", "DOPESHEET_EDITOR", "WINDOW"),  # Handles both Timeline and Dope Sheet
    ("Graph Editor", "GRAPH_EDITOR", "WINDOW"),
]


def register():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return

    # Clear any previous instances before registering
    unregister()

    for km_name, space_type, region_type in KM_DEFINITIONS:
        km = kc.keymaps.new(name=km_name, space_type=space_type, region_type=region_type)

        # Ctrl + Wheel Up: Next Frame (+1)
        kmi_up = km.keymap_items.new("screen.frame_offset", type="WHEELUPMOUSE", value="PRESS", ctrl=True)
        kmi_up.properties.delta = 1
        addon_keymaps.append((km, kmi_up))

        # Ctrl + Wheel Down: Previous Frame (-1)
        kmi_down = km.keymap_items.new("screen.frame_offset", type="WHEELDOWNMOUSE", value="PRESS", ctrl=True)
        kmi_down.properties.delta = -1
        addon_keymaps.append((km, kmi_down))

        # Shift + Ctrl + Wheel Up: Jump 10 Frames Forward
        kmi_shift_up = km.keymap_items.new("screen.frame_offset", type="WHEELUPMOUSE", value="PRESS", ctrl=True, shift=True)
        kmi_shift_up.properties.delta = 10
        addon_keymaps.append((km, kmi_shift_up))

        # Shift + Ctrl + Wheel Down: Jump 10 Frames Backward
        kmi_shift_down = km.keymap_items.new("screen.frame_offset", type="WHEELDOWNMOUSE", value="PRESS", ctrl=True, shift=True)
        kmi_shift_down.properties.delta = -10
        addon_keymaps.append((km, kmi_shift_down))


def unregister():
    for km, kmi in addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    addon_keymaps.clear()


if __name__ == "__main__":
    register()
