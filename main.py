import json, time as pytime, math
import numpy as np
from ursina import (
    Ursina, Entity, Vec3, color, Text, window, time,
    mouse, application, camera
)
from ursina.prefabs.first_person_controller import FirstPersonController


# =========================
# Config
# =========================
CHUNK_SIZE = 32            # 每个区块边长（越大越“稀疏”）
GROUND_THICKNESS = 4       # 地面厚度（防穿模）
LOAD_RADIUS = 2            # 玩家周围加载半径（2 => 5x5=25块）
MAX_OBS_PER_CHUNK = 18     # 每块最多障碍数（性能开关）
PORTAL_PROB = 0.10         # 每块生成传送门概率
PORTAL_TRIGGER_DIST = 2.2  # 触发距离
PORTAL_COOLDOWN = 1.5      # 触发冷却秒


# =========================
# Deterministic RNG
# =========================
def rng_from(*items):
    seed = 0
    for x in items:
        seed = (seed * 1315423911 + int(x) * 2654435761) & 0xFFFFFFFF
    return np.random.default_rng(seed)


# =========================
# Layer / Phase effects
# =========================
def phase_offset(layer: int, phase: float):
    """Phase 让世界整体轻微漂移，形成“同层不同相位”的状态变化。"""
    rng = rng_from(layer, 9090)
    a = 0.6 + 0.4 * rng.random()
    b = 0.8 + 1.2 * rng.random()
    w = 2 * np.pi * phase
    return Vec3(a * math.sin(w), 0.0, b * math.cos(w))


def chunk_key(cx: int, cz: int):
    return (int(cx), int(cz))


def world_to_chunk(x: float, z: float):
    cx = math.floor(x / CHUNK_SIZE)
    cz = math.floor(z / CHUNK_SIZE)
    return int(cx), int(cz)


def chunk_center(cx: int, cz: int):
    return Vec3(cx * CHUNK_SIZE + CHUNK_SIZE / 2, 0, cz * CHUNK_SIZE + CHUNK_SIZE / 2)


# =========================
# Chunk generation (Minecraft-like)
# =========================
def generate_chunk_obstacles(layer: int, cx: int, cz: int):
    """
    生成一个区块内的障碍（box collider），返回:
    [(base_pos: Vec3, scale: Vec3, col: color), ...]
    """
    rng = rng_from(layer, cx, cz, 2025)

    density = 0.55 + 0.35 * (rng_from(layer, 777).random())
    n = int(rng.integers(6, MAX_OBS_PER_CHUNK + 1) * density)

    theme = [
        color.azure, color.orange, color.lime, color.magenta,
        color.cyan, color.yellow, color.violet, color.red
    ]
    base_theme = theme[layer % len(theme)]

    out = []
    x0 = cx * CHUNK_SIZE
    z0 = cz * CHUNK_SIZE

    for _ in range(n):
        px = x0 + rng.random() * CHUNK_SIZE
        pz = z0 + rng.random() * CHUNK_SIZE
        py = -2 + rng.random() * 5.0

        sx = 1.0 + 4.0 * rng.random()
        sy = 1.0 + 8.0 * rng.random()
        sz = 1.0 + 4.0 * rng.random()

        if rng.random() < 0.22:
            sx *= 5.0
            sz *= 0.5

        col = base_theme * (0.6 + 0.4 * rng.random())
        out.append((Vec3(px, py, pz), Vec3(sx, sy, sz), col))

    return out


def should_spawn_portal(layer: int, cx: int, cz: int):
    rng = rng_from(layer, cx, cz, 4242)
    return rng.random() < PORTAL_PROB


def portal_position(layer: int, cx: int, cz: int):
    rng = rng_from(layer, cx, cz, 8888)
    center = chunk_center(cx, cz)
    dx = (rng.random() - 0.5) * (CHUNK_SIZE * 0.5)
    dz = (rng.random() - 0.5) * (CHUNK_SIZE * 0.5)
    return Vec3(center.x + dx, -0.5, center.z + dz)


# =========================
# App setup
# =========================
app = Ursina()

window.title = "Geomancer_FPV — Parallel Worlds FPS Explorer"
window.color = color.black
window.size = (1280, 720)
window.fullscreen = False
window.borderless = False
window.vsync = True
application.target_fps = 60

# 关掉右上角计数器（如需可改为 True）
window.fps_counter.enabled = False
window.entity_counter.enabled = False
window.collider_counter.enabled = False

player = FirstPersonController(y=2)
player.speed = 7
player.cursor.visible = True

hud = Text(
    text="",
    parent=camera.ui,
    origin=(-0.5, 0.5),
    scale=0.85,
    background=True
)

toast = Text(
    text="",
    parent=camera.ui,
    origin=(0, 0),
    scale=1.0,
    color=color.white,
    background=True
)
toast.enabled = False
toast_timer = 0.0


def layout_ui():
    """根据屏幕宽高比自动把 HUD 放到左上角，避免全屏被裁剪。"""
    margin = 0.02
    hud.x = -0.5 * camera.aspect_ratio + margin
    hud.y = 0.5 - margin
    toast.x = 0
    toast.y = 0.40


# =========================
# State
# =========================
MODE = 3
layer = 0
phase = 0.0
auto_phase = False
auto_phase_speed = 0.10

recording = True
traj = []
accum = 0.0
sample_dt = 0.05

phase_drift = Vec3(0, 0, 0)

chunks = {}
portal_cooldown = 0.0


# =========================
# Helpers
# =========================
def show_toast(msg: str, seconds: float = 1.2):
    global toast_timer
    toast.text = msg
    toast.enabled = True
    toast_timer = seconds


def clear_all_chunks():
    global chunks
    for _, data in list(chunks.items()):
        try:
            data["ground"].disable()
            data["ground"].parent = None
        except:
            pass
        for e, _ in data.get("obstacles", []):
            try:
                e.disable()
                e.parent = None
            except:
                pass
        p = data.get("portal", None)
        if p is not None:
            try:
                p.disable()
                p.parent = None
            except:
                pass
    chunks = {}


def reset_player_safe():
    player.y = 3
    if hasattr(player, "velocity"):
        player.velocity = Vec3(0, 0, 0)


def export_traj(path="trajectory.json"):
    payload = {
        "meta": {
            "created_at": pytime.strftime("%Y-%m-%d %H:%M:%S"),
            "sample_dt": sample_dt,
            "note": "Infinite-chunk FPS exploration; layer/phase define parallel-world coordinates.",
        },
        "trajectory": traj
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# =========================
# Chunk loading/unloading
# =========================
def create_chunk(cx: int, cz: int):
    key = chunk_key(cx, cz)
    if key in chunks:
        return

    center = chunk_center(cx, cz)
    ground = Entity(
        model='cube',
        position=Vec3(center.x, -GROUND_THICKNESS, center.z),
        scale=(CHUNK_SIZE, GROUND_THICKNESS * 2, CHUNK_SIZE),
        collider='box',
        color=color.dark_gray
    )

    obs_specs = generate_chunk_obstacles(layer, cx, cz)
    obs_entities = []
    for base_pos, sc, col in obs_specs:
        e = Entity(
            model='cube',
            position=base_pos,
            scale=sc,
            collider='box',
            color=col
        )
        obs_entities.append((e, base_pos))

    portal_ent = None
    portal_pos = None
    if should_spawn_portal(layer, cx, cz):
        portal_pos = portal_position(layer, cx, cz)

        portal_ent = Entity(
            model='cube',
            position=portal_pos,
            scale=(1.2, 4.2, 0.4),
            collider=None,
            color=color.rgba(180, 90, 255, 160),
        )
        Entity(
            model='cube',
            position=portal_pos + Vec3(0, 0, 0.25),
            scale=(1.35, 4.35, 0.05),
            collider=None,
            color=color.rgba(240, 240, 255, 120)
        )

    chunks[key] = {
        "ground": ground,
        "obstacles": obs_entities,
        "portal": portal_ent,
        "portal_pos": portal_pos
    }


def unload_far_chunks(center_cx: int, center_cz: int):
    to_remove = []
    for (cx, cz), _ in chunks.items():
        if abs(cx - center_cx) > LOAD_RADIUS or abs(cz - center_cz) > LOAD_RADIUS:
            to_remove.append((cx, cz))

    for key in to_remove:
        data = chunks.pop(key, None)
        if data is None:
            continue
        try:
            data["ground"].disable()
            data["ground"].parent = None
        except:
            pass
        for e, _ in data.get("obstacles", []):
            try:
                e.disable()
                e.parent = None
            except:
                pass
        p = data.get("portal", None)
        if p is not None:
            try:
                p.disable()
                p.parent = None
            except:
                pass


def ensure_chunks_loaded():
    cx, cz = world_to_chunk(player.x, player.z)
    for dx in range(-LOAD_RADIUS, LOAD_RADIUS + 1):
        for dz in range(-LOAD_RADIUS, LOAD_RADIUS + 1):
            create_chunk(cx + dx, cz + dz)
    unload_far_chunks(cx, cz)


# =========================
# Mode / input
# =========================
def set_mode(m: int):
    global MODE, layer, phase
    MODE = m
    if MODE == 3:
        layer = 0
        phase = 0.0
        show_toast("3D: locked to Layer=0, Phase=0", 1.2)
    elif MODE == 4:
        phase = 0.0
        show_toast("4D: Layer unlocked (Q/E). Phase locked.", 1.2)
    else:
        show_toast("5D: Layer + Phase unlocked (Q/E, R/F).", 1.2)

    clear_all_chunks()
    reset_player_safe()


def warp_to_next_layer():
    global layer, portal_cooldown
    if MODE < 4:
        show_toast("Portal inactive in 3D. Press 2 or 3 to unlock Layer.", 1.5)
        return

    layer += 1
    portal_cooldown = PORTAL_COOLDOWN
    show_toast(f"Portal warp! -> Layer {layer}", 1.5)

    clear_all_chunks()
    reset_player_safe()


def update_hud():
    loaded = len(chunks)
    hud.text = (
        f"Mode: {MODE}D  (1/2/3)\n"
        f"Layer(w): {layer}   (Q/E)\n"
        f"Phase(v): {phase:.2f} (R/F)\n"
        f"AutoPhase: {'ON' if auto_phase else 'OFF'} (G)\n"
        f"Chunks loaded: {loaded}  (radius={LOAD_RADIUS})\n"
        f"REC: {'ON' if recording else 'OFF'} (T)\n"
        f"Export: P | Clear: O\n"
        f"Fullscreen: F11 | Mouse: TAB | Exit: ESC\n"
        f"Pos: ({player.x:.1f}, {player.y:.1f}, {player.z:.1f})\n"
        f"WASD move | Mouse look | Space jump | Shift sprint"
    )


def input(key):
    global layer, phase, recording, traj, auto_phase

    if key in ('escape', 'esc'):
        application.quit()

    if key == 'tab':
        mouse.locked = not mouse.locked
        mouse.visible = not mouse.locked

    if key == 'f11':
        window.fullscreen = not window.fullscreen

    if key == 'g':
        auto_phase = not auto_phase
        show_toast(f"AutoPhase: {'ON' if auto_phase else 'OFF'}", 1.0)

    if key == '1':
        set_mode(3)
    if key == '2':
        set_mode(4)
    if key == '3':
        set_mode(5)

    if MODE >= 4:
        if key == 'q':
            layer = max(0, layer - 1)
            clear_all_chunks()
            reset_player_safe()
            show_toast(f"Layer -> {layer}", 1.0)
        if key == 'e':
            layer = layer + 1
            clear_all_chunks()
            reset_player_safe()
            show_toast(f"Layer -> {layer}", 1.0)

    if MODE >= 5:
        if key == 'r':
            phase = max(0.0, phase - 0.05)
        if key == 'f':
            phase = min(1.0, phase + 0.05)

    if key == 't':
        recording = not recording

    if key == 'o':
        traj = []

    if key == 'p':
        export_traj("trajectory.json")
        print("Saved trajectory.json")
        show_toast("Saved trajectory.json", 1.0)


# =========================
# Update loop
# =========================
def update():
    global accum, phase_drift, portal_cooldown, phase, toast_timer

    layout_ui()

    if toast.enabled:
        toast_timer -= time.dt
        if toast_timer <= 0:
            toast.enabled = False

    ensure_chunks_loaded()

    if MODE >= 5 and auto_phase:
        phase = (phase + auto_phase_speed * time.dt) % 1.0

    if MODE >= 5:
        phase_drift = phase_offset(layer, phase)
    else:
        phase_drift = Vec3(0, 0, 0)

    for data in chunks.values():
        for e, base_pos in data.get("obstacles", []):
            e.position = base_pos + phase_drift * 0.25

    if player.y < -60:
        reset_player_safe()

    if portal_cooldown > 0:
        portal_cooldown -= time.dt
    else:
        for data in chunks.values():
            ppos = data.get("portal_pos", None)
            if ppos is None:
                continue
            if (player.position - ppos).length() < PORTAL_TRIGGER_DIST:
                warp_to_next_layer()
                break

    if recording:
        accum += time.dt
        while accum >= sample_dt:
            accum -= sample_dt
            traj.append({
                "t": float(pytime.time()),
                "mode": int(MODE),
                "layer": int(layer),
                "phase": float(phase),
                "pos": [float(player.x), float(player.y), float(player.z)],
                "rot": [float(player.rotation_x), float(player.rotation_y), float(player.rotation_z)],
                "speed": float(player.speed),
            })

    update_hud()


# =========================
# Start
# =========================
mouse.locked = True
mouse.visible = False
set_mode(3)
app.run()