import glob
import os
import re
import sys
import shutil
import json
import uuid
import hashlib
import struct
import subprocess
import zipfile
from urllib.parse import quote

import requests
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QDialog, QFileDialog, QMessageBox, QProgressBar, QSpinBox,
    QDialogButtonBox, QFrame, QScrollArea,
)
from PySide6.QtGui import QPixmap, QIcon, QFont, QPainter, QPainterPath
from PySide6.QtCore import Qt, QThread, Signal
import minecraft_launcher_lib
import minecraft_launcher_lib.command as _mll_command
from minecraft_launcher_lib.forge import install_forge_version, forge_to_installed_version

# ── Patch Forge 1.20+ "values" key in arguments ───────────────────────────────
_orig_get_arguments = _mll_command.get_arguments


def _get_arguments_with_values_key(data, versionData, path, options, classpath):
    fixed = []
    for item in data:
        if isinstance(item, dict) and "value" not in item and "values" in item:
            item = {**item, "value": item["values"]}
        fixed.append(item)
    return _orig_get_arguments(fixed, versionData, path, options, classpath)


_mll_command.get_arguments = _get_arguments_with_values_key


def _patch_minecraft_command_line(command: list[str]) -> list[str]:
    client_id = str(uuid.uuid4())
    out = [p.replace("${clientid}", client_id).replace("${auth_xuid}", "0") for p in command]
    try:
        i = out.index("--assetIndex")
        if i + 1 < len(out) and out[i + 1] == "legacy":
            out[i + 1] = "5"
    except ValueError:
        pass
    try:
        j = out.index("--userType")
        if j + 1 < len(out) and out[j + 1] == "msa":
            out[j + 1] = "legacy"
    except ValueError:
        pass
    return out


def _fix_forge_client_json_assets(mc_dir: str, version_id: str) -> None:
    p = os.path.join(mc_dir, "versions", version_id, f"{version_id}.json")
    if not os.path.isfile(p):
        return
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("assets") != "legacy":
            return
        data["assets"] = "5"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except (OSError, json.JSONDecodeError):
        pass


# ── Constants ──────────────────────────────────────────────────────────────────
FIXED_VERSION       = "1.20.1"
FORGE_MAVEN_ID      = "1.20.1-47.4.20"
FIXED_FORGE         = forge_to_installed_version(FORGE_MAVEN_ID)
SERVER_ADDRESS      = "normal-challenges.gl.joinmc.link"
SERVER_DISPLAY_NAME = "Mystryx"
DEFAULT_RAM_MB      = 4096
GITHUB_RAW          = "https://raw.githubusercontent.com/Creatt7777/MystryxUpdater/main"
MANIFEST_URL        = f"{GITHUB_RAW}/manifest.json"
CHANGELOG_URL           = f"{GITHUB_RAW}/changelogs"
GITHUB_BG_ACTIVE_URL    = f"{GITHUB_RAW}/backgrounds/active_background.txt"
GITHUB_BG_RAW_BASE      = f"{GITHUB_RAW}/backgrounds"
_MYSTRYX_BG_STATE_FILE  = ".mystryx_active_background.txt"
# Fonds saisonniers connus sur le dépôt GitHub (nettoyage des anciens fichiers locaux)
_KNOWN_SEASONAL_BACKGROUNDS = frozenset({
    "summer.jpg", "autumn.jpg", "winter.jpg", "spring.png",
})
DEFAULT_MODS_DOWNLOAD_BASE = (
    "https://media.githubusercontent.com/media/Creatt7777/MystryxUpdater/main/mods"
)
DEFAULT_SERVER_ONLY_MOD_NAMES = frozenset({
    "antixray-forge-1.4.6+1.20.1.jar",
    "no-creeper-grief-v1.2.2.1.jar",
})
MCEF_FORGE_MOD = {
    "name": "mcef-forge-2.1.6-1.20.1.jar",
    "hash": "341d01e98e3835162cf8eb439e20dbfe",
    "url": "https://cdn.modrinth.com/data/TObQ0HxZ/versions/x91l6OKB/mcef-forge-2.1.6-1.20.1.jar",
}

# ── FancyMenu constants ────────────────────────────────────────────────────────
_FANCYMENU_TITLE_SCREEN_CLASS = "net.minecraft.client.gui.screens.TitleScreen"
_FANCYMENU_TITLE_SCREEN_ID      = "title_screen"  # identifiant universel FancyMenu v3
_FANCYMENU_THEME_OGG            = "mystryx_theme.ogg"
_FANCYMENU_TITLE_LOGO_ASSET     = "mystryx_title_logo.png"
_TITLE_SCREEN_LOGO_URL          = "https://i.postimg.cc/FRKrpszX/minecraft-title.png"
_FANCYMENU_DEFAULT_BG           = "summer.jpg"  # fallback si GitHub injoignable
# Éléments « deep » du Title Screen à masquer (vanilla_button ne suffit pas — cf. FM v3)
_FANCYMENU_HIDDEN_DEEP_ELEMENTS: tuple[str, ...] = (
    "title_screen_logo",
    "title_screen_branding",
    "title_screen_splash",
    "title_screen_realms_notification",
)

_FANCYMENU_MANAGED_BOOLS: dict[str, bool] = {
    "modpack_mode": True,
    "show_customization_overlay": False,
    "show_welcome_screen": False,
    "show_debug_overlay": False,
    "play_vanilla_menu_music": False,
}
_FANCYMENU_BOOL_SECTION: dict[str, str] = {
    "modpack_mode": "customization",
    "show_customization_overlay": "customization",
    "show_welcome_screen": "tutorial",
    "show_debug_overlay": "debug_overlay",
    "play_vanilla_menu_music": "general",
}


# ── Resource root (handles frozen/PyInstaller builds) ─────────────────────────
def _launcher_resource_root() -> str:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return meipass
        base = os.path.dirname(sys.executable)
        internal = os.path.join(base, "_internal")
        if os.path.isdir(internal):
            return internal
        return base
    return os.path.dirname(os.path.abspath(__file__))


def _launcher_extra_client_mods() -> list[dict]:
    return [MCEF_FORGE_MOD]


# ── FancyMenu layout helpers ───────────────────────────────────────────────────
def _fancymenu_menu_background_image_block(filename: str) -> str:
    """Bloc menu_background image locale."""
    source = f"[source:local]/config/fancymenu/assets/{filename}"
    return (
        "menu_background {\n"
        "  instance_identifier = mystryx_menu_bg_image\n"
        "  background_type = image\n"
        "  show_background = true\n"
        f"  image_path = {source}\n"
        "  slide = false\n"
        "  repeat_texture = false\n"
        "  parallax = false\n"
        "  parallax_intensity_x = 0.02\n"
        "  parallax_intensity_y = 0.02\n"
        "  invert_parallax = false\n"
        "  restart_animated_on_menu_load = true\n"
        "}\n\n"
    )


def _fancymenu_hidden_deep_element(element_type: str) -> str:
    """Masque logo / branding / splash vanilla via deep_element (format FancyMenu v3)."""
    return (
        "deep_element {\n"
        f"  element_type = {element_type}\n"
        f"  instance_identifier = deep:{element_type}\n"
        "  appearance_delay = no_delay\n"
        "  appearance_delay_seconds = 1.0\n"
        "  fade_in_v2 = no_fading\n"
        "  fade_in_speed = 1.0\n"
        "  fade_out = no_fading\n"
        "  fade_out_speed = 1.0\n"
        "  base_opacity = 1.0\n"
        "  auto_sizing = false\n"
        "  auto_sizing_base_screen_width = 0\n"
        "  auto_sizing_base_screen_height = 0\n"
        "  sticky_anchor = false\n"
        "  anchor_point = vanilla\n"
        "  x = 0\n"
        "  y = 0\n"
        "  width = 1\n"
        "  height = 1\n"
        "  stretch_x = false\n"
        "  stretch_y = false\n"
        "  stay_on_screen = true\n"
        "  is_hidden = true\n"
        "}\n\n"
    )


def _fancymenu_title_logo_image_block() -> str:
    """Logo Mystryx centré en haut (taille fixe — auto_sizing gonfle tout en ultrawide)."""
    source = f"[source:local]/config/fancymenu/assets/{_FANCYMENU_TITLE_LOGO_ASSET}"
    return (
        "element {\n"
        "  element_type = image\n"
        "  instance_identifier = mystryx_title_logo_image\n"
        "  anchor_point = top-centered\n"
        "  x = -70\n"
        "  y = 24\n"
        "  width = 140\n"
        "  height = 134\n"
        "  auto_sizing = false\n"
        "  auto_sizing_base_screen_width = 0\n"
        "  auto_sizing_base_screen_height = 0\n"
        "  sticky_anchor = false\n"
        "  stay_on_screen = true\n"
        f"  source = {source}\n"
        "  repeat_texture = false\n"
        "  nine_slice_texture = false\n"
        "  nine_slice_texture_border_x = 5\n"
        "  nine_slice_texture_border_y = 5\n"
        "  restart_animated_on_menu_load = true\n"
        "  image_tint = #FFFFFF\n"
        "  appearance_delay = no_delay\n"
        "  disappearance_delay = no_delay\n"
        "  fade_in_v2 = no_fading\n"
        "  fade_out = no_fading\n"
        "  base_opacity = 1.0\n"
        "  enable_parallax = false\n"
        "  invert_parallax = false\n"
        "  animated_offset_x = 0\n"
        "  animated_offset_y = 0\n"
        "  load_once_per_session = false\n"
        "  layer_hidden_in_editor = false\n"
        "  advanced_rotation_mode = false\n"
        "  rotation_degrees = 0.0\n"
        "  should_be_affected_by_decoration_overlays = false\n"
        "}\n\n"
    )


def _fancymenu_title_layout_content(server_address: str, bg_filename: str = _FANCYMENU_DEFAULT_BG) -> str:
    """Génère le contenu complet du layout FancyMenu title screen."""
    join_block = "mystryx_join_block"
    join_act   = "mystryx_join_action"
    lr_el      = "mystryx_join_lr_el"
    lr_wa      = "mystryx_join_lr_wa"

    def vanilla_btn(instance_id: str, noop_id: str, anchor: str,
                    x: int, y: int, w: int, h: int,
                    is_hidden: bool, fade: str = "no_fading") -> str:
        return (
            "vanilla_button {\n"
            f"  button_element_executable_block_identifier = mystryx_noop_{noop_id}\n"
            f"  [executable_block:mystryx_noop_{noop_id}][type:generic] = [executables:]\n"
            "  underline_label_on_hover = false\n"
            "  transparent_background = false\n"
            "  restartbackgroundanimations = true\n"
            "  nine_slice_custom_background = false\n"
            "  navigatable = true\n"
            f"  widget_active_state_requirement_container_identifier = mystryx_lr_wa_{noop_id}\n"
            f"  [loading_requirement_container_meta:mystryx_lr_wa_{noop_id}] = [groups:][instances:]\n"
            "  is_template = false\n"
            "  template_share_with = buttons\n"
            "  nine_slice_slider_handle = false\n"
            "  element_type = vanilla_button\n"
            f"  instance_identifier = {instance_id}\n"
            "  appearance_delay = no_delay\n"
            "  disappearance_delay = no_delay\n"
            f"  fade_in_v2 = {fade}\n"
            "  fade_out = no_fading\n"
            f"  anchor_point = {anchor}\n"
            f"  x = {x}\n"
            f"  y = {y}\n"
            f"  width = {w}\n"
            f"  height = {h}\n"
            "  auto_sizing = false\n"
            "  auto_sizing_base_screen_width = 1920\n"
            "  auto_sizing_base_screen_height = 1080\n"
            "  sticky_anchor = false\n"
            "  stay_on_screen = false\n"
            f"  element_loading_requirement_container_identifier = mystryx_lr_el_{noop_id}\n"
            f"  [loading_requirement_container_meta:mystryx_lr_el_{noop_id}] = [groups:][instances:]\n"
            "  enable_parallax = false\n"
            "  invert_parallax = false\n"
            "  animated_offset_x = 0\n"
            "  animated_offset_y = 0\n"
            "  load_once_per_session = false\n"
            "  layer_hidden_in_editor = false\n"
            "  advanced_rotation_mode = false\n"
            "  rotation_degrees = 0.0\n"
            "  should_be_affected_by_decoration_overlays = true\n"
            "  base_opacity = 1.0\n"
            "  label_scale = 1.0\n"
            "  label_shadow = true\n"
            "  nine_slice_border_x = 5\n"
            "  nine_slice_border_y = 5\n"
            "  nine_slice_slider_handle_border_x = 5\n"
            "  nine_slice_slider_handle_border_y = 5\n"
            "  automated_button_clicks = 0\n"
            f"  is_hidden = {str(is_hidden).lower()}\n"
            "}\n\n"
        )

    return (
        "type = fancymenu_layout\n\n"
        "layout-meta {\n"
        f"  identifier = {_FANCYMENU_TITLE_SCREEN_ID}\n"
        "  render_custom_elements_behind_vanilla = false\n"
        "  last_edited_time = 0\n"
        "  is_enabled = true\n"
        "  randommode = false\n"
        "  randomgroup = 1\n"
        "  randomonlyfirsttime = false\n"
        "  layout_index = 1\n"
        "}\n\n"
        + _fancymenu_menu_background_image_block(bg_filename)
        + "customization {\n"
        "  action = backgroundoptions\n"
        "  keepaspectratio = false\n"
        "}\n\n"
        "scroll_list_customization {\n"
        "  show_screen_background_overlay_on_custom_background = false\n"
        "  apply_vanilla_background_blur = false\n"
        "}\n\n"
        "layout_action_executable_blocks {\n"
        "}\n\n"
        + _fancymenu_title_logo_image_block()
        + "element {\n"
        "  element_type = custom_button\n"
        "  instance_identifier = mystryx_btn_rejoindre\n"
        "  anchor_point = mid-centered\n"
        "  x = -100\n"
        "  y = 40\n"
        "  width = 200\n"
        "  height = 20\n"
        "  label = Rejoindre\n"
        "  navigatable = true\n"
        "  underline_label_on_hover = true\n"
        "  transparent_background = false\n"
        "  restartbackgroundanimations = true\n"
        "  nine_slice_custom_background = false\n"
        "  stay_on_screen = true\n"
        f"  element_loading_requirement_container_identifier = {lr_el}\n"
        f"  [loading_requirement_container_meta:{lr_el}] = [groups:][instances:]\n"
        f"  widget_active_state_requirement_container_identifier = {lr_wa}\n"
        f"  [loading_requirement_container_meta:{lr_wa}] = [groups:][instances:]\n"
        f"  button_element_executable_block_identifier = {join_block}\n"
        f"  [executable_block:{join_block}][type:generic] = [executables:{join_act};]\n"
        f"  [executable_action_instance:{join_act}][action_type:joinserver] = {server_address}\n"
        "  appearance_delay = no_delay\n"
        "  disappearance_delay = no_delay\n"
        "  fade_in_v2 = no_fading\n"
        "  fade_out = no_fading\n"
        "  base_opacity = 1.0\n"
        "  auto_sizing = false\n"
        "  auto_sizing_base_screen_width = 0\n"
        "  auto_sizing_base_screen_height = 0\n"
        "  sticky_anchor = false\n"
        "  stretch_x = false\n"
        "  stretch_y = false\n"
        "  enable_parallax = false\n"
        "  invert_parallax = false\n"
        "  animated_offset_x = 0\n"
        "  animated_offset_y = 0\n"
        "  load_once_per_session = false\n"
        "  layer_hidden_in_editor = false\n"
        "  advanced_rotation_mode = false\n"
        "  rotation_degrees = 0.0\n"
        "  is_template = false\n"
        "  template_share_with = buttons\n"
        "  nine_slice_slider_handle = false\n"
        "  nine_slice_border_x = 5\n"
        "  nine_slice_border_y = 5\n"
        "  nine_slice_slider_handle_border_x = 5\n"
        "  nine_slice_slider_handle_border_y = 5\n"
        "  label_scale = 1.0\n"
        "  label_shadow = true\n"
        "  should_be_affected_by_decoration_overlays = true\n"
        "}\n\n"
        + "".join(_fancymenu_hidden_deep_element(et) for et in _FANCYMENU_HIDDEN_DEEP_ELEMENTS)
        + vanilla_btn("mc_titlescreen_options_button", "options", "mid-centered", -70, 65, 140, 20, False, "no_fading")
        + vanilla_btn("mc_titlescreen_quit_button",   "quit",    "mid-centered", -50, 90, 100, 20, False, "no_fading")
        + vanilla_btn("mc_titlescreen_singleplayer_button", "sp",  "vanilla", 220, 132, 200, 20, True)
        + vanilla_btn("mc_titlescreen_multiplayer_button",  "mp",  "vanilla", 220, 156, 200, 20, True)
        + vanilla_btn("mc_titlescreen_realms_button",       "rl",  "vanilla", 322, 180,  98, 20, True)
        + vanilla_btn("mc_titlescreen_language_button",     "lng", "vanilla", 196, 216,  20, 20, True)
        + vanilla_btn("mc_titlescreen_accessibility_button","acc", "vanilla", 424, 216,  20, 20, True)
        + vanilla_btn("forge_titlescreen_mods_button",      "fmb", "vanilla", 220, 180,  98, 20, True)
        + vanilla_btn("title_screen_copyright_button",      "cr",  "vanilla", 442, 327, 196, 10, True)
    )

# ── FancyMenu options ──────────────────────────────────────────────────────────
def _set_fm_v3_bool_option(content: str, key: str, value: bool) -> tuple[str, bool]:
    val = "true" if value else "false"
    pat = rf"^(\s*[Bb]:{re.escape(key)}\s*=\s*)'[^']*'(\s*;?\s*)$"
    new_content, n = re.subn(
        pat,
        lambda m: f"{m.group(1)}'{val}'{m.group(2)}",
        content,
        count=1,
        flags=re.MULTILINE,
    )
    return new_content, n > 0


def _inject_fm_v3_bool_option(content: str, key: str, value: bool) -> str:
    val = "true" if value else "false"
    line = f"B:{key} = '{val}';"
    section = _FANCYMENU_BOOL_SECTION.get(key, "customization")
    marker = f"##[{section}]"
    if marker in content:
        return content.replace(marker, f"{marker}\n{line}", 1)
    return content.rstrip() + f"\n\n##[{section}]\n{line}\n"


def _strip_legacy_fm_option_lines(content: str) -> str:
    out: list[str] = []
    legacy_keys = set(_FANCYMENU_MANAGED_BOOLS)
    for line in content.splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*", line)
        if m and m.group(1) in legacy_keys and not line.strip().startswith("B:"):
            continue
        if line.strip().startswith("# Mystryx Launcher"):
            continue
        out.append(line)
    return "\n".join(out).rstrip() + "\n"


def ensure_fancymenu_options(mc_dir: str) -> None:
    fm_dir = os.path.join(mc_dir, "config", "fancymenu")
    path   = os.path.join(fm_dir, "options.txt")
    try:
        os.makedirs(fm_dir, exist_ok=True)
        content = (
            open(path, encoding="utf-8").read()
            if os.path.isfile(path)
            else "##[general]\n\n##[customization]\n\n##[tutorial]\n\n##[debug_overlay]\n"
        )
        content = _strip_legacy_fm_option_lines(content)
        for key, value in _FANCYMENU_MANAGED_BOOLS.items():
            content, found = _set_fm_v3_bool_option(content, key, value)
            if not found:
                content = _inject_fm_v3_bool_option(content, key, value)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content if content.endswith("\n") else content + "\n")
    except OSError:
        pass


def ensure_fancymenu_customizable_menus(mc_dir: str) -> None:
    fm_dir = os.path.join(mc_dir, "config", "fancymenu")
    path   = os.path.join(fm_dir, "customizablemenus.txt")
    title_block   = f"{_FANCYMENU_TITLE_SCREEN_CLASS} {{\n}}\n"
    default_file  = f"type = customizablemenus\n\n{title_block}"
    try:
        os.makedirs(fm_dir, exist_ok=True)
        existing = open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
    except OSError:
        existing = ""
    if re.search(r"net\.minecraft\.client\.gui\.screens\.TitleScreen\s*\{", existing):
        return
    stripped = existing.strip()
    if not stripped:
        new_content = default_file
    elif re.search(r"type\s*=\s*customizablemenus", stripped, re.I):
        new_content = stripped + "\n\n" + title_block
    else:
        new_content = default_file
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_content)
    except OSError:
        pass


# ── FancyMenu assets ───────────────────────────────────────────────────────────
# (pas de video bundlée — background géré par sync_active_background)


def ensure_fancymenu_theme_music(mc_dir: str) -> None:
    """Copie mystryx_theme.ogg bundlé dans .minecraft/config/fancymenu/assets/."""
    bundled = os.path.join(_launcher_resource_root(), _FANCYMENU_THEME_OGG)
    if not os.path.isfile(bundled):
        return
    assets_dir = os.path.join(mc_dir, "config", "fancymenu", "assets")
    try:
        os.makedirs(assets_dir, exist_ok=True)
        shutil.copy2(bundled, os.path.join(assets_dir, _FANCYMENU_THEME_OGG))
    except OSError:
        pass


def ensure_fancymenu_title_logo(mc_dir: str) -> None:
    """Télécharge le logo titre en local (FancyMenu charge mal les images web en modpack)."""
    assets_dir = os.path.join(mc_dir, "config", "fancymenu", "assets")
    dest = os.path.join(assets_dir, _FANCYMENU_TITLE_LOGO_ASSET)
    try:
        os.makedirs(assets_dir, exist_ok=True)
        if os.path.isfile(dest) and os.path.getsize(dest) > 4096:
            return
        r = requests.get(_TITLE_SCREEN_LOGO_URL, timeout=30)
        if r.status_code != 200:
            return
        part = dest + ".part"
        with open(part, "wb") as f:
            f.write(r.content)
        os.replace(part, dest)
    except OSError:
        pass


def ensure_create_client_config(mc_dir: str) -> None:
    """Désactive le bouton / logo Create sur le menu titre (mainMenuConfigButtonRow = 0)."""
    path = os.path.join(mc_dir, "config", "create-client.toml")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        content = open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
        if re.search(r"^\s*mainMenuConfigButtonRow\s*=", content, re.MULTILINE):
            content, _ = re.subn(
                r"^(\s*mainMenuConfigButtonRow\s*=\s*)\d+",
                r"\g<1>0",
                content,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            block = "\n# Mystryx Launcher — masque le bouton Create sur le menu titre\nmainMenuConfigButtonRow = 0\n"
            content = (content.rstrip() + block) if content.strip() else block.lstrip()
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content if content.endswith("\n") else content + "\n")
    except OSError:
        pass


# ── FancyMenu layout ───────────────────────────────────────────────────────────
def _migrate_join_server_address(path: str) -> None:
    """Met à jour l'adresse du serveur dans un layout existant."""
    join_line = (
        f"[executable_action_instance:mystryx_join_action][action_type:joinserver] = "
        f"{SERVER_ADDRESS}"
    )
    try:
        with open(path, encoding="utf-8") as f:
            s = f.read()
        s2, n = re.subn(
            r"\[executable_action_instance:mystryx_join_action\]\[action_type:joinserver\]\s*=\s*[^\n]+",
            join_line,
            s,
            count=1,
        )
        if n and s2 != s:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(s2)
    except OSError:
        pass


def _migrate_layout_index(path: str) -> None:
    """S'assure que layout_index = 1."""
    try:
        with open(path, encoding="utf-8") as f:
            s = f.read()
        s2 = re.sub(
            r"(layout-meta\s*\{[^}]*?)layout_index\s*=\s*\d+",
            r"\1layout_index = 1",
            s,
            count=1,
            flags=re.DOTALL,
        )
        if s2 != s:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(s2)
    except OSError:
        pass


def _migrate_disable_parallax(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as f:
            s = f.read()
        s2 = s.replace("  parallax = true\n", "  parallax = false\n")
        s2 = re.sub(r"  parallax_intensity = 0\.015\n", "  parallax_intensity = 0.02\n", s2)
        if s2 != s:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(s2)
    except OSError:
        pass


def _migrate_title_screen_layout(path: str, mc_dir: str) -> None:
    """Met à jour un layout existant : deep_element pour masquer vanilla, logo Mystryx centré."""
    ensure_fancymenu_title_logo(mc_dir)
    local_logo = f"[source:local]/config/fancymenu/assets/{_FANCYMENU_TITLE_LOGO_ASSET}"
    try:
        with open(path, encoding="utf-8") as f:
            s = f.read()
    except OSError:
        return

    s2 = s
    s2 = re.sub(
        r"^(\s*identifier\s*=\s*)net\.minecraft\.client\.gui\.screens\.TitleScreen\s*$",
        rf"\g<1>{_FANCYMENU_TITLE_SCREEN_ID}",
        s2,
        count=1,
        flags=re.MULTILINE,
    )

    # Ancienne méthode (vanilla_button) inefficace pour logo/branding — on la retire
    s2 = re.sub(
        r"vanilla_button \{[\s\S]*?instance_identifier = title_screen_(?:logo|branding|splash|realms_notification)\b[\s\S]*?\}\n\n",
        "",
        s2,
    )

    missing_deep = [
        et for et in _FANCYMENU_HIDDEN_DEEP_ELEMENTS
        if f"deep:{et}" not in s2 and f"element_type = {et}" not in s2
    ]
    if missing_deep:
        insert = "".join(_fancymenu_hidden_deep_element(et) for et in missing_deep)
        anchor = "layout_action_executable_blocks {\n}\n\n"
        if anchor in s2:
            s2 = s2.replace(anchor, anchor + insert, 1)
        else:
            s2 = s2.rstrip() + "\n\n" + insert

    for element_type in _FANCYMENU_HIDDEN_DEEP_ELEMENTS:
        s2 = re.sub(
            rf"(element_type = {re.escape(element_type)}[\s\S]*?is_hidden = )\w+",
            r"\1true",
            s2,
            count=1,
        )

    if "instance_identifier = mystryx_title_logo_image" in s2:
        s2 = re.sub(
            r"(instance_identifier = mystryx_title_logo_image[\s\S]*?source = )[^\n]+",
            rf"\g<1>{local_logo}",
            s2,
            count=1,
        )
        s2 = re.sub(
            r"(instance_identifier = mystryx_title_logo_image[\s\S]*?anchor_point = )\S+",
            r"\1top-centered",
            s2,
            count=1,
        )
        s2 = re.sub(
            r"(instance_identifier = mystryx_title_logo_image[\s\S]*?auto_sizing = )\w+",
            r"\1false",
            s2,
            count=1,
        )
        s2 = re.sub(
            r"(instance_identifier = mystryx_title_logo_image[\s\S]*?auto_sizing_base_screen_width = )\d+",
            r"\g<1>0",
            s2,
            count=1,
        )
        s2 = re.sub(
            r"(instance_identifier = mystryx_title_logo_image[\s\S]*?auto_sizing_base_screen_height = )\d+",
            r"\g<1>0",
            s2,
            count=1,
        )
        s2 = re.sub(
            r"(instance_identifier = mystryx_title_logo_image[\s\S]*?  width = )\d+",
            r"\g<1>140",
            s2,
            count=1,
        )
        s2 = re.sub(
            r"(instance_identifier = mystryx_title_logo_image[\s\S]*?  height = )\d+",
            r"\g<1>134",
            s2,
            count=1,
        )
        s2 = re.sub(
            r"(instance_identifier = mystryx_title_logo_image[\s\S]*?  x = )-?\d+",
            r"\g<1>-70",
            s2,
            count=1,
        )
        s2 = re.sub(
            r"(instance_identifier = mystryx_title_logo_image[\s\S]*?  y = )-?\d+",
            r"\g<1>24",
            s2,
            count=1,
        )
    else:
        anchor = "layout_action_executable_blocks {\n}\n\n"
        if anchor in s2:
            s2 = s2.replace(anchor, anchor + _fancymenu_title_logo_image_block(), 1)

    if s2 != s:
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(s2)
        except OSError:
            pass


# ── FancyMenu background GitHub sync ──────────────────────────────────────────
def _patch_fancymenu_background_source(mc_dir: str, filename: str) -> None:
    """Met à jour proprement la ligne image_path dans le layout FancyMenu."""
    layout_path = os.path.join(
        mc_dir, "config", "fancymenu", "customization", "mystryx_title_join_only.txt"
    )
    if not os.path.isfile(layout_path):
        return
    
    # CORRECTIF : On remet pour que FancyMenu trouve le fichier sur le PC
    source = f"/config/fancymenu/assets/{filename}"
    
    try:
        with open(layout_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
        modified = False
        for i, line in enumerate(lines):
            # On repère la ligne du background, peu importe les espaces autour du "="
            if "image_path" in line and "mystryx_menu_bg_image" not in line:
                lines[i] = f"  image_path = {source}\n"
                modified = True
                break
                
        if modified:
            with open(layout_path, "w", encoding="utf-8", newline="\n") as f:
                f.writelines(lines)
    except OSError:
        pass

def _remove_local_background_file(assets_dir: str, filename: str) -> None:
    if not filename:
        return
    path = os.path.join(assets_dir, filename)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _cleanup_old_background_assets(assets_dir: str, active_name: str, previous_name: str) -> None:
    """Supprime l'ancien fond et les autres fonds saisonniers non actifs."""
    protected = {_FANCYMENU_TITLE_LOGO_ASSET, _FANCYMENU_THEME_OGG, _MYSTRYX_BG_STATE_FILE}
    if previous_name and previous_name != active_name:
        _remove_local_background_file(assets_dir, previous_name)
    for name in _KNOWN_SEASONAL_BACKGROUNDS:
        if name != active_name and name not in protected:
            _remove_local_background_file(assets_dir, name)


def sync_active_background(mc_dir: str) -> str:
    """
    Lit backgrounds/active_background.txt sur GitHub, télécharge le fond indiqué,
    supprime l'ancien et retourne le nom de fichier pour le layout FancyMenu.
    """
    active_name = _FANCYMENU_DEFAULT_BG
    assets_dir = os.path.join(mc_dir, "config", "fancymenu", "assets")
    state_path = os.path.join(assets_dir, _MYSTRYX_BG_STATE_FILE)
    try:
        import time
        import urllib.request

        # Un timestamp unique (ex: 1716584300) pour briser le cache de GitHub
        timestamp = int(time.time())

        # 1. Récupérer le nom du background actif (Anti-cache activé)
        url_active = GITHUB_BG_ACTIVE_URL + f"?ts={timestamp}"
        with urllib.request.urlopen(url_active, timeout=10) as r:
            name = r.read().decode("utf-8").strip()
        if name:
            active_name = name

        os.makedirs(assets_dir, exist_ok=True)
        previous_name = ""
        if os.path.isfile(state_path):
            previous_name = open(state_path, encoding="utf-8").read().strip()

        # 2. Télécharger l'image (Anti-cache activé ici aussi !)
        dest = os.path.join(assets_dir, active_name)
        if not os.path.isfile(dest) or previous_name != active_name:
            # On ajoute ?ts=... à la fin de l'URL de l'image pour bypass le cache GitHub
            url = f"{GITHUB_BG_RAW_BASE}/{quote(active_name)}?ts={timestamp}"
            part = dest + ".part"
            
            dl = requests.get(url, stream=True, timeout=60)
            if dl.status_code == 200:
                try:
                    with open(part, "wb") as f:
                        shutil.copyfileobj(dl.raw, f)
                    dl.close()
                    os.replace(part, dest)
                except Exception:
                    if os.path.exists(part):
                        os.remove(part)
                    return active_name
            else:
                return active_name

        # 3. Nettoyer les anciens backgrounds locaux
        _cleanup_old_background_assets(assets_dir, active_name, previous_name)
        with open(state_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(active_name + "\n")

    except Exception:
        pass  # Silencieux pour ne pas crash
    return active_name

def ensure_fancymenu_title_layout(mc_dir: str, bg_filename: str | None = None) -> None:
    ensure_fancymenu_customizable_menus(mc_dir)
    ensure_fancymenu_options(mc_dir)
    ensure_fancymenu_theme_music(mc_dir)
    ensure_fancymenu_title_logo(mc_dir)
    ensure_create_client_config(mc_dir)

    dest_dir = os.path.join(mc_dir, "config", "fancymenu", "customization")
    path = os.path.join(dest_dir, "mystryx_title_join_only.txt")
    
    # Résolution du nom de l'image (via GitHub ou fallback)
    bg = bg_filename or _FANCYMENU_DEFAULT_BG
    
    try:
        os.makedirs(dest_dir, exist_ok=True)
        if not os.path.isfile(path):
            header = (
                "# Layout Mystryx Launcher — modifiable dans FancyMenu.\n"
                "# Supprime ce fichier et relance le launcher pour recréer le défaut.\n\n"
            )
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(header + _fancymenu_title_layout_content(SERVER_ADDRESS, bg))
            return  # Fichier créé, pas besoin de migrer
    except OSError:
        return

    # ── MIGRATIONS ET MISES À JOUR FORMAT ──
    _migrate_join_server_address(path)
    _migrate_layout_index(path)
    _migrate_disable_parallax(path)
    _migrate_title_screen_layout(path, mc_dir)

    # Force la mise à jour de l'image active dans le fichier texte de FancyMenu
    _patch_fancymenu_background_source(mc_dir, bg)

    # Supprime l'ancien layout enhancements s'il traîne
    old = os.path.join(dest_dir, "mystryx_enhancements.txt")
    try:
        if os.path.isfile(old):
            os.remove(old)
    except OSError:
        pass

# ── Misc helpers ───────────────────────────────────────────────────────────────
def _humanize_download_status(message: str) -> str:
    msg = message.strip()
    if msg.startswith("Download "):
        name = msg[9:].strip()
        if len(name) > 18 and sum(c.isdigit() for c in name) >= 6:
            return "Téléchargement des fichiers Minecraft / Forge…"
        return f"Téléchargement : {name}"
    if msg.startswith("Running processor"):
        return "Installation Forge (configuration)…"
    if msg == "Download Libraries":
        return "Téléchargement des bibliothèques…"
    if msg == "Download Assets":
        return "Téléchargement des assets Minecraft…"
    if msg.startswith("Install java"):
        return "Installation du Java Minecraft…"
    if msg == "Installation complete":
        return "Installation terminée ✓"
    return msg


def _is_valid_png_file(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(8) == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def get_appdata_path() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return os.path.join(base, "MystryxLauncher")
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~"), "Library", "Application Support", "MystryxLauncher"
        )
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return os.path.join(xdg, "MystryxLauncher")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "MystryxLauncher")


def _tail_file(path: str, max_lines: int = 45) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "(fichier vide)" if not lines else "".join(lines[-max_lines:])
    except OSError:
        return "(lecture du log impossible)"


SETTINGS_FILE = os.path.join(get_appdata_path(), "settings.json")


def save_settings(**kwargs):
    data = load_settings()
    data.update(kwargs)
    os.makedirs(get_appdata_path(), exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def is_valid_zip_jar(path: str) -> bool:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            zf.namelist()
        return True
    except (zipfile.BadZipFile, OSError):
        return False


def find_invalid_mod_jars(mods_dir: str) -> list[str]:
    bad: list[str] = []
    if not os.path.isdir(mods_dir):
        return bad
    for name in os.listdir(mods_dir):
        if not name.lower().endswith(".jar"):
            continue
        p = os.path.join(mods_dir, name)
        if os.path.isfile(p) and not is_valid_zip_jar(p):
            bad.append(name)
    return bad


# ── Minecraft / Forge helpers ──────────────────────────────────────────────────
def _client_mod_entries(manifest: dict) -> list[dict]:
    out: list[dict] = []
    for m in manifest.get("mods", []):
        if not isinstance(m, dict):
            continue
        name = m.get("name")
        if not name or "hash" not in m:
            continue
        if m.get("serverOnly") or name in DEFAULT_SERVER_ONLY_MOD_NAMES:
            continue
        out.append(m)
    return out


def _mod_download_url(name: str, entry: dict, mods_base: str) -> str:
    if entry.get("url"):
        return str(entry["url"])
    return f"{mods_base.rstrip('/')}/{quote(name, safe='')}"


def _find_installed_forge_1201(mc_dir: str) -> str | None:
    vd = os.path.join(mc_dir, "versions")
    if not os.path.isdir(vd):
        return None
    candidates: list[tuple[str, tuple[int, ...]]] = []
    for name in os.listdir(vd):
        jp = os.path.join(vd, name, f"{name}.json")
        if not os.path.isfile(jp):
            continue
        try:
            with open(jp, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        main     = (meta.get("mainClass") or "").lower()
        inherits = meta.get("inheritsFrom") or ""
        if (
            FIXED_VERSION not in name
            and inherits != FIXED_VERSION
            and not str(meta.get("id", "")).startswith(FIXED_VERSION)
        ):
            continue
        if "bootstraplauncher" not in main and "forge" not in name.lower():
            continue
        m = re.search(r"forge[_-](\d+)\.(\d+)\.(\d+)", name, re.I) or \
            re.search(r"forge[_-](\d+)\.(\d+)\.(\d+)", str(meta.get("id", "")), re.I)
        key = tuple(map(int, m.groups())) if m else (0, 0, 0)
        candidates.append((name, key))
    if not candidates:
        return None
    names = [c[0] for c in candidates]
    if FIXED_FORGE in names:
        return FIXED_FORGE
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]


def _mojang_runtime_java_executable(mc_dir: str) -> str | None:
    gamma = os.path.join(mc_dir, "runtime", "java-runtime-gamma")
    if not os.path.isdir(gamma):
        return None
    if os.name == "nt":
        p = os.path.join(gamma, "windows", "java-runtime-gamma", "bin", "java.exe")
        return p if os.path.isfile(p) else None
    if sys.platform == "darwin":
        pat = os.path.join(
            gamma, "mac-os", "*", "java-runtime-gamma",
            "jre.bundle", "Contents", "Home", "bin", "java",
        )
        found = sorted(glob.glob(pat))
        return found[0] if found else None
    pat = os.path.join(gamma, "linux", "*", "java-runtime-gamma", "bin", "java")
    found = sorted(glob.glob(pat))
    return found[0] if found else None


def _ensure_server_in_list(mc_dir: str) -> None:
    servers_dat = os.path.join(mc_dir, "servers.dat")

    def nbt_string(name: str, value: str) -> bytes:
        nb, vb = name.encode("utf-8"), value.encode("utf-8")
        return b'\x08' + struct.pack(">H", len(nb)) + nb + struct.pack(">H", len(vb)) + vb

    def build_dat(entries: list[dict]) -> bytes:
        compound_list = b""
        for e in entries:
            compound_list += nbt_string("name", e["name"]) + nbt_string("ip", e["ip"]) + b'\x00'
        return (
            b'\x0a' + struct.pack(">H", 0)
            + b'\x09' + struct.pack(">H", len("servers")) + b"servers"
            + b'\x0a' + struct.pack(">i", len(entries))
            + compound_list + b'\x00'
        )

    if os.path.exists(servers_dat):
        with open(servers_dat, "rb") as f:
            if SERVER_ADDRESS.encode("utf-8") in f.read():
                return
    try:
        with open(servers_dat, "wb") as f:
            f.write(build_dat([{"name": SERVER_DISPLAY_NAME, "ip": SERVER_ADDRESS}]))
    except OSError as e:
        print(f"servers.dat error: {e}")


def _ensure_launcher_profile(mc_dir: str, version_id: str) -> None:
    profile_path = os.path.join(mc_dir, "launcher_profiles.json")
    if os.path.exists(profile_path):
        try:
            with open(profile_path, encoding="utf-8") as f:
                json.load(f)
            return
        except json.JSONDecodeError:
            pass
    data = {
        "profiles": {
            "mystryx": {
                "name": "Mystryx",
                "lastVersionId": version_id,
                "type": "custom",
            }
        },
        "selectedProfile": "mystryx",
        "clientToken": str(uuid.uuid4()),
        "launcherVersion": {"name": "MystryxLauncher", "format": 21},
    }
    with open(profile_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


def _fix_forge_missing_jar(mc_dir: str, version_id: str) -> str | None:
    forge_dir = os.path.join(mc_dir, "versions", version_id)
    forge_jar = os.path.join(forge_dir, f"{version_id}.jar")
    if os.path.exists(forge_jar):
        return None
    base_version = version_id.split("-")[0]
    vanilla_jar  = os.path.join(mc_dir, "versions", base_version, f"{base_version}.jar")
    if os.path.exists(vanilla_jar):
        os.makedirs(forge_dir, exist_ok=True)
        shutil.copy(vanilla_jar, forge_jar)
        return None
    return f"Installe d'abord Minecraft {base_version} (vanilla) puis relance le launcher."


# ── Threads ────────────────────────────────────────────────────────────────────
class ModUpdater(QThread):
    progress = Signal(str)
    done     = Signal(bool)

    def __init__(self, mods_dir: str):
        super().__init__()
        self.mods_dir = mods_dir

    def run(self):
        try:
            os.makedirs(self.mods_dir, exist_ok=True)
            self.progress.emit("Vérification des mods…")

            r = requests.get(MANIFEST_URL, timeout=15)
            if r.status_code != 200:
                self.progress.emit("Manifest inaccessible, lancement quand même…")
                self.done.emit(True)
                return

            manifest     = r.json()
            client_entries = _client_mod_entries(manifest)
            remote_mods  = {m["name"]: m for m in client_entries}
            for extra in _launcher_extra_client_mods():
                remote_mods.setdefault(extra["name"], extra)
            mods_base = str(manifest.get("modsDownloadBase") or DEFAULT_MODS_DOWNLOAD_BASE)

            for f in os.listdir(self.mods_dir):
                if f.endswith(".jar") and f not in remote_mods:
                    os.remove(os.path.join(self.mods_dir, f))
                    self.progress.emit(f"Supprimé : {f}")

            total = len(remote_mods)
            for idx, (name, meta) in enumerate(remote_mods.items(), 1):
                local_path  = os.path.join(self.mods_dir, name)
                remote_hash = meta["hash"]
                if os.path.exists(local_path):
                    if md5_file(local_path) == remote_hash and is_valid_zip_jar(local_path):
                        continue
                    try:
                        os.remove(local_path)
                    except OSError:
                        pass

                self.progress.emit(f"Mod {idx}/{total} : {name}")
                url       = _mod_download_url(name, meta, mods_base)
                part_path = local_path + ".part"
                try:
                    dl = requests.get(url, stream=True, timeout=60)
                    if dl.status_code != 200:
                        self.progress.emit(f"Erreur : {name} (HTTP {dl.status_code})")
                        continue
                    with open(part_path, "wb") as f:
                        shutil.copyfileobj(dl.raw, f)
                    dl.close()
                    if md5_file(part_path) != remote_hash:
                        os.remove(part_path)
                        self.progress.emit(f"Erreur : {name} (hash MD5 incorrect)")
                        continue
                    if not is_valid_zip_jar(part_path):
                        os.remove(part_path)
                        self.progress.emit(f"Erreur : {name} (archive ZIP invalide)")
                        continue
                    os.replace(part_path, local_path)
                except Exception as e:
                    try:
                        if os.path.exists(part_path):
                            os.remove(part_path)
                    except OSError:
                        pass
                    self.progress.emit(f"Erreur : {name} ({e})")

            self.progress.emit("Mods à jour ✓")
            self.done.emit(True)

        except Exception as e:
            self.progress.emit(f"Erreur update : {e}")
            self.done.emit(True)


class GameLauncher(QThread):
    progress      = Signal(str)
    game_starting = Signal()
    finished      = Signal(dict)

    def __init__(self, username: str, mc_dir: str):
        super().__init__()
        self.username = username
        self.mc_dir   = mc_dir

    def _forge_callback(self) -> dict:
        return {
            "setStatus":   lambda msg: self.progress.emit(_humanize_download_status(str(msg))),
            "setMax":      lambda _: None,
            "setProgress": lambda _: None,
        }

    def run(self):
        result: dict = {"ok": False}
        mc_dir = self.mc_dir
        try:
            ram_mb  = int(load_settings().get("ram_mb", DEFAULT_RAM_MB))
            xms_mb  = min(ram_mb, 2048)
            options = {
                "username":       self.username,
                "uuid":           "12345678-1234-1234-1234-123456789abc",
                "token":          "faketoken",
                "jvmArguments":   [f"-Xmx{ram_mb}M", f"-Xms{xms_mb}M"],
                "launcherName":   "MystryxLauncher",
                "launcherVersion": "1.0",
            }

            version_id = _find_installed_forge_1201(mc_dir)
            if version_id is None:
                self.progress.emit("Installation Forge (téléchargement)…")
                install_forge_version(FORGE_MAVEN_ID, mc_dir, callback=self._forge_callback())
                version_id = FIXED_FORGE

            self.progress.emit(f"Préparation ({version_id})…")
            _fix_forge_client_json_assets(mc_dir, version_id)
            _ensure_launcher_profile(mc_dir, version_id)
            jar_err = _fix_forge_missing_jar(mc_dir, version_id)
            if jar_err:
                result["missing_vanilla"] = jar_err
                return
            _ensure_server_in_list(mc_dir)
            bg_name = sync_active_background(mc_dir)
            ensure_fancymenu_title_layout(mc_dir, bg_filename=bg_name)
            _patch_fancymenu_background_source(mc_dir, bg_name)

            command = minecraft_launcher_lib.command.get_minecraft_command(
                version_id, mc_dir, options
            )
            command = _patch_minecraft_command_line(command)
            java_rt = _mojang_runtime_java_executable(mc_dir)
            if java_rt and os.path.isfile(java_rt):
                command[0] = java_rt

            run_log = os.path.join(get_appdata_path(), "minecraft_last_run.log")
            os.makedirs(get_appdata_path(), exist_ok=True)
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

            self.progress.emit("Lancement de Minecraft…")
            self.game_starting.emit()

            with open(run_log, "w", encoding="utf-8") as logf:
                proc = subprocess.Popen(
                    command, cwd=mc_dir,
                    stdout=logf, stderr=subprocess.STDOUT,
                    creationflags=flags,
                )
                ret = proc.wait()

            result.update({"ok": True, "exit_code": ret, "run_log": run_log, "mc_dir": mc_dir})

        except Exception as e:
            result["error"] = f"Impossible de lancer Minecraft :\n{e}\n\nType: {type(e).__name__}"
        finally:
            self.finished.emit(result)


# ── UI helpers ─────────────────────────────────────────────────────────────────
def _circular_avatar_pixmap(source: QPixmap, size: int = 80) -> QPixmap:
    scaled = source.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    out    = QPixmap(size, size)
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing)
    clip = QPainterPath()
    clip.addEllipse(0, 0, size, size)
    painter.setClipPath(clip)
    painter.drawPixmap((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    painter.end()
    return out


# ── Dialogs ────────────────────────────────────────────────────────────────────
class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Paramètres")
        self.setFixedWidth(360)
        self.setStyleSheet(parent.load_styles() if parent else "")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("Paramètres")
        title.setFont(QFont("Segoe UI", 16, QFont.Bold))
        layout.addWidget(title)

        ram_label = QLabel("Mémoire allouée (RAM)")
        ram_label.setObjectName("muted")
        layout.addWidget(ram_label)

        self.ram_spin = QSpinBox()
        self.ram_spin.setRange(1024, 16384)
        self.ram_spin.setSingleStep(512)
        self.ram_spin.setSuffix(" Mo")
        self.ram_spin.setFixedHeight(40)
        self.ram_spin.setValue(int(load_settings().get("ram_mb", DEFAULT_RAM_MB)))
        layout.addWidget(self.ram_spin)

        hint = QLabel("Valeur stable recommandée : 4096 Mo pour Forge 1.20.1.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def ram_mb(self) -> int:
        return self.ram_spin.value()


class ChangelogFetcher(QThread):
    loaded = Signal(str)
    failed = Signal(str)

    def run(self):
        try:
            r = requests.get(CHANGELOG_URL, timeout=15)
            if r.status_code != 200:
                self.failed.emit(f"Impossible de charger le changelog (HTTP {r.status_code}).")
                return
            self.loaded.emit(r.text.strip() or "Aucune note pour le moment.")
        except requests.RequestException as e:
            self.failed.emit(f"Erreur réseau : {e}")


class ChangelogDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Changelog — Mystryx")
        self.resize(640, 480)
        self.setStyleSheet(parent.load_styles() if parent else "")
        self._fetcher: ChangelogFetcher | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title  = QLabel("Nouveautés")
        title.setFont(QFont("Segoe UI", 16, QFont.Bold))
        header.addWidget(title)
        header.addStretch()
        refresh_btn = QPushButton("Actualiser")
        refresh_btn.setFixedHeight(32)
        refresh_btn.clicked.connect(self._load_changelog)
        header.addWidget(refresh_btn)
        layout.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body = QLabel("Chargement…")
        self.body.setWordWrap(True)
        self.body.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setObjectName("changelogBody")
        self.scroll.setWidget(self.body)
        layout.addWidget(self.scroll, stretch=1)

        hint = QLabel("Source : MystryxUpdater sur GitHub")
        hint.setObjectName("muted")
        layout.addWidget(hint)

        close_btn = QDialogButtonBox()
        close_btn.addButton("Continuer", QDialogButtonBox.AcceptRole)
        close_btn.accepted.connect(self.accept)
        layout.addWidget(close_btn)

        self._load_changelog()

    def _set_body_text(self, text: str):
        self.body.setText(text)
        self.body.adjustSize()

    def _load_changelog(self):
        self._set_body_text("Chargement des nouveautés…")
        if self._fetcher and self._fetcher.isRunning():
            return
        self._fetcher = ChangelogFetcher()
        self._fetcher.loaded.connect(self._set_body_text)
        self._fetcher.failed.connect(self._set_body_text)
        self._fetcher.start()


# ── Main window ────────────────────────────────────────────────────────────────
class MystryxLauncher(QWidget):

    def __init__(self):
        super().__init__()
        self.appdata_dir = get_appdata_path()
        self.assets_dir  = os.path.join(self.appdata_dir, "assets")
        self.icon_path   = os.path.join(
            self.assets_dir, "logo.png" if sys.platform == "darwin" else "logo.ico"
        )
        self.ensure_assets_exist()
        self.setWindowTitle("Mystryx Launcher")
        self.setWindowIcon(QIcon(self.icon_path))
        self.setFixedSize(440, 540)
        self.setStyleSheet(self.load_styles())
        self._build_ui()
        ensure_fancymenu_options(minecraft_launcher_lib.utils.get_minecraft_directory())

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(18)

        # ── Header ──
        header    = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title = QLabel("Mystryx")
        title.setObjectName("brandTitle")
        title.setFont(QFont("Segoe UI", 26, QFont.Bold))
        subtitle = QLabel("Launcher")
        subtitle.setObjectName("muted")
        subtitle.setFont(QFont("Segoe UI", 11))
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        header.addLayout(title_col)
        header.addStretch()
        for label, tip, slot in [
            ("📋", "Changelog / nouveautés", self.open_changelog),
            ("⚙",  "Paramètres",             self.open_settings),
        ]:
            btn = QPushButton(label)
            btn.setObjectName("iconButton")
            btn.setFixedSize(40, 40)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            header.addWidget(btn)
        root.addLayout(header)

        # ── Card ──
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        card_layout.setSpacing(14)

        avatar_row = QHBoxLayout()
        avatar_row.setAlignment(Qt.AlignCenter)
        avatar_row.setSpacing(14)
        self.profile_label = QLabel()
        self.profile_label.setFixedSize(80, 80)
        self.profile_label.setAlignment(Qt.AlignCenter)
        self.profile_label.setStyleSheet(
            "QLabel { border-radius: 40px; border: 2px solid #4d6aff; background-color: #12141f; }"
        )
        self.profile_label.setCursor(Qt.PointingHandCursor)
        self.profile_label.mousePressEvent = self.change_profile_photo
        self.load_profile_photo()
        avatar_text = QVBoxLayout()
        avatar_text.setSpacing(4)
        avatar_title = QLabel("Photo de profil")
        avatar_title.setFont(QFont("Segoe UI", 12, QFont.Bold))
        avatar_hint = QLabel("Clique pour changer — affichée en rond")
        avatar_hint.setObjectName("muted")
        avatar_hint.setWordWrap(True)
        change_btn = QPushButton("Choisir une image")
        change_btn.setFixedHeight(34)
        change_btn.clicked.connect(lambda: self.change_profile_photo(None))
        avatar_text.addWidget(avatar_title)
        avatar_text.addWidget(avatar_hint)
        avatar_text.addWidget(change_btn)
        avatar_row.addWidget(self.profile_label)
        avatar_row.addLayout(avatar_text)
        card_layout.addLayout(avatar_row)

        pseudo_label = QLabel("Pseudo Minecraft")
        pseudo_label.setObjectName("muted")
        card_layout.addWidget(pseudo_label)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Ton pseudo")
        self.username_input.setFixedHeight(44)
        self.username_input.setText(load_settings().get("username", ""))
        card_layout.addWidget(self.username_input)

        ram_mb = int(load_settings().get("ram_mb", DEFAULT_RAM_MB))
        self.ram_hint_label = QLabel(f"RAM : {ram_mb} Mo")
        self.ram_hint_label.setObjectName("muted")
        self.ram_hint_label.setAlignment(Qt.AlignRight)
        card_layout.addWidget(self.ram_hint_label)
        root.addWidget(card)

        # ── Status / progress ──
        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setObjectName("footer")
        root.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setVisible(False)
        self.progress_bar.setObjectName("progressBar")
        root.addWidget(self.progress_bar)

        # ── Play button ──
        self.play_button = QPushButton("▶  JOUER")
        self.play_button.setObjectName("playButton")
        self.play_button.setFixedHeight(52)
        self.play_button.clicked.connect(self.on_play_clicked)
        root.addWidget(self.play_button)

        # ── Footer ──
        footer_col = QVBoxLayout()
        footer_col.setSpacing(4)
        footer = QLabel(f"Serveur : {SERVER_DISPLAY_NAME}")
        footer.setAlignment(Qt.AlignCenter)
        footer.setObjectName("footer")
        footer_col.addWidget(footer)
        changelog_link = QPushButton("Voir le changelog")
        changelog_link.setObjectName("changelogLink")
        changelog_link.setCursor(Qt.PointingHandCursor)
        changelog_link.clicked.connect(self.open_changelog)
        footer_col.addWidget(changelog_link, alignment=Qt.AlignCenter)
        root.addLayout(footer_col)

    def load_styles(self) -> str:
        return """
        QWidget { background-color: #0a0c14; color: #e8eaf6; font-family: 'Segoe UI'; font-size: 14px; }
        QFrame#card {
            background-color: #12141f;
            border: 1px solid #252836;
            border-radius: 16px;
        }
        QLabel#brandTitle { color: #ffffff; }
        QLabel#muted { color: #7a8199; font-size: 12px; }
        QLineEdit {
            background-color: #0f111a;
            border: 1px solid #2c2f3a;
            border-radius: 10px;
            padding: 10px 14px;
            color: white;
        }
        QLineEdit:focus { border: 1px solid #4d6aff; }
        QSpinBox {
            background-color: #0f111a;
            border: 1px solid #2c2f3a;
            border-radius: 10px;
            padding: 8px 12px;
            color: white;
        }
        QSpinBox:focus { border: 1px solid #4d6aff; }
        QPushButton {
            background-color: #1a1d29;
            border: none;
            padding: 8px 14px;
            border-radius: 8px;
            font-weight: bold;
            color: white;
        }
        QPushButton:hover { background-color: #232737; }
        QPushButton#iconButton {
            background-color: #12141f;
            border: 1px solid #2c2f3a;
            border-radius: 10px;
            font-size: 18px;
            padding: 0;
        }
        QPushButton#iconButton:hover { background-color: #1e2233; border-color: #4d6aff; }
        QPushButton#playButton {
            background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #3b5cff, stop:1 #5b7cff);
            font-size: 16px;
            border-radius: 14px;
            letter-spacing: 2px;
        }
        QPushButton#playButton:hover { background-color: #4d6aff; }
        QPushButton#playButton:pressed { background-color: #2a47e0; }
        QPushButton:disabled { background-color: #1a1d29; color: #555870; }
        QLabel#footer { color: #555870; font-size: 11px; }
        QPushButton#changelogLink {
            background: transparent;
            color: #5a6280;
            font-size: 11px;
            font-weight: normal;
            padding: 0;
            border: none;
        }
        QPushButton#changelogLink:hover { color: #4d6aff; }
        QLabel#changelogBody { background: transparent; color: #e8eaf6; font-size: 14px; padding: 4px 2px; }
        QScrollArea { background: transparent; border: none; }
        QProgressBar#progressBar { border: none; background: #12141f; border-radius: 3px; }
        QProgressBar#progressBar::chunk { background: #4d6aff; border-radius: 3px; }
        """

    def open_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec() == QDialog.Accepted:
            save_settings(ram_mb=dlg.ram_mb())
            self.ram_hint_label.setText(f"RAM : {dlg.ram_mb()} Mo")

    def open_changelog(self):
        ChangelogDialog(self).exec()

    def load_profile_photo(self):
        path = os.path.join(self.appdata_dir, "profile.png")
        if not os.path.exists(path):
            path = os.path.join(self.assets_dir, "logo.png")
        pixmap = QPixmap(path)
        if not pixmap.isNull():
            self.profile_label.setPixmap(_circular_avatar_pixmap(pixmap, 80))

    def change_profile_photo(self, _event):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Sélectionner une photo", "", "Images (*.png *.jpg *.jpeg *.bmp)"
        )
        if not file_path:
            return
        pixmap = QPixmap(file_path)
        if pixmap.isNull():
            QMessageBox.warning(self, "Image invalide", "Impossible de charger cette image.")
            return
        if max(pixmap.width(), pixmap.height()) > 512:
            pixmap = pixmap.scaled(512, 512, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        dest = os.path.join(self.appdata_dir, "profile.png")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        pixmap.save(dest, "PNG")
        self.load_profile_photo()

    def on_play_clicked(self):
        username = self.username_input.text().strip()
        if not username:
            QMessageBox.warning(self, "Pseudo manquant", "Entre ton pseudo Minecraft.")
            return
        save_settings(username=username)
        self.play_button.setEnabled(False)
        self.progress_bar.setVisible(True)

        mc_dir   = minecraft_launcher_lib.utils.get_minecraft_directory()
        mods_dir = os.path.join(mc_dir, "mods")
        self.updater = ModUpdater(mods_dir)
        self.updater.progress.connect(self._on_update_progress)
        self.updater.done.connect(lambda _ok: self._after_mod_update(username, mc_dir))
        self.updater.start()

    def _after_mod_update(self, username: str, mc_dir: str):
        mods_dir = os.path.join(mc_dir, "mods")
        invalid  = find_invalid_mod_jars(mods_dir)
        if invalid:
            self.progress_bar.setVisible(False)
            self.play_button.setEnabled(True)
            self.play_button.setText("▶  JOUER")
            self.status_label.setText("")
            QMessageBox.warning(
                self, "Mod(s) corrompu(s)",
                "Un ou plusieurs fichiers .jar ne sont pas des archives valides.\n\n"
                "Fichiers concernés :\n"
                + "\n".join(f"• {n}" for n in invalid[:20])
                + ("\n…" if len(invalid) > 20 else "")
                + "\n\nSupprime-les du dossier mods ou relance pour retélécharger.",
            )
            return
        self._start_game_launch(username, mc_dir)

    def _on_update_progress(self, msg: str):
        self.status_label.setText(msg)

    def _start_game_launch(self, username: str, mc_dir: str):
        self.status_label.setText("Préparation…")
        self.progress_bar.setVisible(True)
        self._game_thread = GameLauncher(username, mc_dir)
        self._game_thread.progress.connect(self._on_update_progress)
        self._game_thread.game_starting.connect(self.hide, Qt.ConnectionType.BlockingQueuedConnection)
        self._game_thread.finished.connect(self._on_game_launch_finished)
        self._game_thread.start()

    def _on_game_launch_finished(self, result: dict):
        self.show()
        self.progress_bar.setVisible(False)
        self.play_button.setText("▶  JOUER")
        self.play_button.setEnabled(True)
        self.status_label.setText("")

        if result.get("missing_vanilla"):
            QMessageBox.warning(self, "Version de base manquante", result["missing_vanilla"])
            return
        if result.get("error"):
            QMessageBox.critical(self, "Erreur", result["error"])
            return
        if not result.get("ok"):
            return

        ret = int(result.get("exit_code", 0))
        if ret != 0:
            run_log = result.get("run_log", "")
            mc_dir  = result.get("mc_dir", "")
            tail    = _tail_file(run_log) if run_log else ""
            QMessageBox.warning(
                self, "Minecraft s'est arrêté",
                f"Code de sortie : {ret}\n\n"
                f"Log interne : {run_log}\n"
                f"Log Minecraft : {os.path.join(mc_dir, 'logs', 'debug.log')}\n\n"
                f"--- Fin du log interne ---\n{tail}",
            )

    def ensure_assets_exist(self):
        os.makedirs(self.assets_dir, exist_ok=True)
        for name, url in {
            "logo.png": "https://i.postimg.cc/52CkwNyC/icon.jpg",
            "logo.ico": "https://github.com/Creatt7777/MystryxLauncher/blob/main/icon.ico",
        }.items():
            path = os.path.join(self.assets_dir, name)
            if not os.path.exists(path):
                try:
                    r = requests.get(url, stream=True, timeout=10)
                    if r.status_code == 200:
                        with open(path, "wb") as f:
                            shutil.copyfileobj(r.raw, f)
                except Exception as e:
                    print(f"Asset error {name}: {e}")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    os.makedirs(get_appdata_path(), exist_ok=True)
    os.chdir(get_appdata_path())
    app    = QApplication(sys.argv)
    window = MystryxLauncher()
    window.show()
    QApplication.processEvents()
    ChangelogDialog(window).exec()
    sys.exit(app.exec())