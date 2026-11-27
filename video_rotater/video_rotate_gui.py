#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频旋转器 · VideoRotator v1.0
================================================================
单文件 GUI，零第三方依赖（只用 Python 标准库 + tkinter），离线可用，
拷到任何装了 Python 3.8+ 的 Windows 电脑上双击即可运行。

两种引擎
--------
1) 元数据引擎（内置，纯 Python，无需任何外部程序）
   直接改写 MP4/MOV/M4V 的 tkhd 显示矩阵（display matrix），
   与 ffmpeg 的 `-display_rotation:v:0 -90 -i in.mp4 -c copy out.mp4` 完全等效。
   → 秒级完成、不重新编码、画质零损失。
   → PotPlayer / VLC / mpv / 浏览器 / 手机 / Windows 媒体播放器 等都识别。
2) ffmpeg 引擎（可选，需自备 ffmpeg.exe，可放在本脚本同目录 bin 文件夹下）
   像素级真旋转，输出格式全兼容；同时用于 mkv/avi/wmv 等不支持矩阵的容器。

用法
----
    python video_rotate_gui.py              # 启动图形界面
    python video_rotate_gui.py --selftest   # 自检内置 MP4 引擎（不启动界面）

作者：为「老师」定制  ·  2026
"""

import hashlib
import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback

APP_NAME = "视频旋转器"
APP_VER = "1.0"
CONFIG_NAME = "video_rotate_config.json"

# ---------------------------------------------------------------- 文件类型
MP4_EXTS = {".mp4", ".m4v", ".mov", ".qt", ".3gp", ".3g2"}
VIDEO_EXTS = MP4_EXTS | {".mkv", ".avi", ".wmv", ".flv", ".webm", ".ts",
                         ".m2ts", ".mts", ".mpg", ".mpeg", ".rmvb", ".rm",
                         ".vob", ".ogv", ".asf", ".f4v"}

# ---------------------------------------------------------------- 主题配色
BG = "#17181c"
CARD = "#22242a"
CARD2 = "#2b2e35"
LINE = "#34383f"
FG = "#e8eaed"
MUTED = "#8f959e"
ACCENT = "#3d7eff"
ACCENT_HI = "#5b93ff"
OK = "#38c26b"
WARN = "#e8b339"
ERR = "#ef5350"
CYAN = "#39c5cf"


def script_dir():
    """脚本所在目录（兼容 PyInstaller 打包后的情况）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# ======================================================================
#  一、MP4 / MOV 盒子解析与显示矩阵改写（内置引擎，纯 Python）
# ======================================================================
class MP4Error(Exception):
    pass


def _iter_boxes(buf, start, end):
    """遍历 [start, end) 区间内的同级盒子，产出 (相对偏移, 总大小, 头长, 类型)。"""
    pos = start
    while pos + 8 <= end:
        size = struct.unpack_from(">I", buf, pos)[0]
        btype = bytes(buf[pos + 4:pos + 8])
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                break
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            break
        yield pos, size, hdr, btype
        pos += size


def _find_box(buf, start, end, want):
    for pos, size, hdr, btype in _iter_boxes(buf, start, end):
        if btype == want:
            return pos, size, hdr
    return None


def locate_video_tkhd(path):
    """
    定位第一个视频轨（handler_type == 'vide'）的 tkhd 盒子。

    返回 dict：
        abs_offset  tkhd 盒子在文件中的绝对偏移
        payload     tkhd 载荷起始绝对偏移
        version     0 或 1
        matrix_off  matrix 在文件中的绝对偏移（36 字节）
        width/height  tkhd 记录的显示宽高（像素）
    """
    fsize = os.path.getsize(path)
    with open(path, "rb") as f:
        pos = 0
        moov = None
        while pos + 8 <= fsize:
            f.seek(pos)
            head = f.read(16)
            if len(head) < 8:
                break
            size = struct.unpack(">I", head[:4])[0]
            btype = head[4:8]
            if size == 1:
                if len(head) < 16:
                    break
                size = struct.unpack(">Q", head[8:16])[0]
            elif size == 0:
                size = fsize - pos
            if size < 8:
                break
            if btype == b"moov":
                moov = (pos, size)
                break
            pos += size
        if moov is None:
            raise MP4Error("不是有效的 MP4/MOV：未找到 moov 盒子")
        f.seek(moov[0])
        buf = f.read(moov[1])

    moov_pos, moov_size = moov
    end = len(buf)

    # moov -> trak* -> (mdia -> hdlr).handler == vide
    # 注意：buf 是 moov 盒子的内容，索引 0 对应文件偏移 moov_pos，
    # 因此这里遍历必须用 buf 内相对偏移（从 8 开始），最后再换算成绝对偏移。
    for tpos, tsize, thdr, ttype in _iter_boxes(buf, 8, end):
        if ttype != b"trak":
            continue
        mdia = _find_box(buf, tpos + thdr, tpos + tsize, b"mdia")
        if mdia is None:
            continue
        mpos, msize, mhdr = mdia
        hdlr = _find_box(buf, mpos + mhdr, mpos + msize, b"hdlr")
        if hdlr is None:
            continue
        hpos, hsize, hhdr = hdlr
        handler = bytes(buf[hpos + hhdr + 8: hpos + hhdr + 12])
        if handler != b"vide":
            continue

        tkhd = _find_box(buf, tpos + thdr, tpos + tsize, b"tkhd")
        if tkhd is None:
            raise MP4Error("视频轨缺少 tkhd 盒子")
        kpos, ksize, khdr = tkhd
        payload = kpos + khdr
        version = buf[payload]
        if version == 1:
            matrix_rel = payload + 52
            wh_rel = payload + 88
        elif version == 0:
            matrix_rel = payload + 40
            wh_rel = payload + 76
        else:
            raise MP4Error("不支持的 tkhd 版本 %d" % version)

        if matrix_rel + 36 > kpos + ksize or wh_rel + 8 > kpos + ksize:
            raise MP4Error("tkhd 盒子长度异常")

        width = struct.unpack_from(">I", buf, wh_rel)[0] / 65536.0
        height = struct.unpack_from(">I", buf, wh_rel + 4)[0] / 65536.0
        return {
            "abs_offset": moov_pos + kpos,
            "payload": moov_pos + payload,
            "version": version,
            "matrix_off": moov_pos + matrix_rel,
            "width": int(round(width)),
            "height": int(round(height)),
            "matrix": bytes(buf[matrix_rel:matrix_rel + 36]),
        }
    raise MP4Error("未找到视频轨")


def matrix_to_rotation(matrix):
    """
    把 36 字节显示矩阵换算成角度（度，正=逆时针），并报告缩放/镜像。
    与 ffmpeg av_display_rotation_get() 同构：矩阵里 b=-sinθ，
    所以 θ = -atan2(b, a)。（先前写成 +atan2 会导致整体方向相反）
    """
    a, b, _u, c, d, _v = struct.unpack(">6i", matrix[:24])
    if a == 0 and b == 0:
        return 0.0, False, False
    theta = -math.degrees(math.atan2(b, a))
    mirrored = (a * d - b * c) < 0
    scaled = abs(math.hypot(a, b) - 65536) > 64
    return theta, mirrored, scaled


def rotation_to_matrix(theta):
    """
    角度 → 36 字节显示矩阵。theta 为 ffmpeg display_rotation 语义（正 = 逆时针）。

    ffmpeg libavutil/display.c 的 av_display_rotation_set() 里有一句
    `radians = -angle * M_PI / 180.0`，展开后等价于：
        a = cosθ   b = -sinθ
        c = sinθ   d =  cosθ
    这里必须保留这个负号，否则写出的矩阵方向与 ffmpeg 完全相反。
    """
    r = math.radians(theta)
    cs = int(round(math.cos(r) * 65536))
    sn = int(round(math.sin(r) * 65536))
    return struct.pack(">9i", cs, -sn, 0, sn, cs, 0, 0, 0, 0x40000000)


def read_mp4_meta(path):
    """读取 MP4 的时长/分辨率/旋转角度，失败返回 None。"""
    try:
        info = locate_video_tkhd(path)
    except Exception:
        return None
    theta, mirrored, scaled = matrix_to_rotation(info["matrix"])
    return {
        "width": info["width"],
        "height": info["height"],
        "rotation": theta,
        "mirrored": mirrored,
        "scaled": scaled,
        "duration": _read_mp4_duration(path),
    }


def _read_mp4_duration(path):
    """从 moov/mvhd 读取时长（秒）。失败返回 None。"""
    try:
        fsize = os.path.getsize(path)
        with open(path, "rb") as f:
            pos = 0
            while pos + 8 <= fsize:
                f.seek(pos)
                head = f.read(16)
                if len(head) < 8:
                    return None
                size = struct.unpack(">I", head[:4])[0]
                btype = head[4:8]
                if size == 1:
                    size = struct.unpack(">Q", head[8:16])[0]
                elif size == 0:
                    size = fsize - pos
                if size < 8:
                    return None
                if btype == b"moov":
                    f.seek(pos)
                    buf = f.read(size)
                    mvhd = _find_box(buf, 8, len(buf), b"mvhd")
                    if not mvhd:
                        return None
                    p = mvhd[0] + mvhd[2]
                    ver = buf[p]
                    if ver == 1:
                        ts, du = struct.unpack_from(">IQ", buf, p + 20)
                    else:
                        ts, du = struct.unpack_from(">II", buf, p + 12)
                    return (du / ts) if ts else None
                pos += size
    except Exception:
        return None
    return None


def metadata_rotate(src, dst, delta_deg, log=None):
    """
    内置引擎：复制文件后原地改写显示矩阵。返回 (theta_before, theta_after)。
    delta_deg 采用 ffmpeg display_rotation 约定：负值 = 顺时针。
    """
    info = locate_video_tkhd(src)
    theta0, mirrored, scaled = matrix_to_rotation(info["matrix"])
    base = int(round(theta0 / 90.0)) * 90
    if log:
        if abs(theta0 - base) > 1:
            log("  注意：原文件旋转 %.1f° 非 90 的整数倍，已取整为 %d°" % (theta0, base), "warn")
        if mirrored:
            log("  注意：原文件含镜像矩阵，改写后镜像被清除", "warn")
        if scaled:
            log("  注意：原文件含缩放矩阵，改写后缩放被清除", "warn")

    theta_new = (base + delta_deg) % 360
    if theta_new > 180:
        theta_new -= 360

    shutil.copy2(src, dst)
    with open(dst, "r+b") as f:
        f.seek(info["matrix_off"])
        f.write(rotation_to_matrix(theta_new))
    return base, theta_new


# ======================================================================
#  二、ffmpeg 引擎
# ======================================================================
FFMPEG_CANDIDATES = [
    os.path.join(script_dir(), "ffmpeg.exe"),
    os.path.join(script_dir(), "bin", "ffmpeg.exe"),
    os.path.join(script_dir(), "bin", "ffmpeg", "bin", "ffmpeg.exe"),
    os.path.join(script_dir(), "ffmpeg", "bin", "ffmpeg.exe"),
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
]


def find_binary(name, extra=None):
    """按 环境变量 → 配置文件 → 脚本目录 → PATH → 常见安装位 的顺序查找。"""
    env = os.environ.get("VIDEO_ROTATOR_%s" % name.upper())
    if env and os.path.isfile(env):
        return env
    if extra and os.path.isfile(extra):
        return extra
    for p in FFMPEG_CANDIDATES:
        if os.path.basename(p).lower() == name.lower() + ".exe" and os.path.isfile(p):
            return p
    w = shutil.which(name)
    if w:
        return w
    return None


def ffmpeg_version(exe):
    if not exe:
        return None
    try:
        creation = 0
        if os.name == "nt":
            creation = subprocess.CREATE_NO_WINDOW
        out = subprocess.run([exe, "-version"], capture_output=True, text=True,
                             errors="replace", timeout=15, creationflags=creation).stdout
        m = re.search(r"ffmpeg version (\S+)", out)
        return m.group(1) if m else "unknown"
    except Exception:
        return None


def list_encoders(exe):
    try:
        creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        out = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True,
                             text=True, errors="replace", timeout=20,
                             creationflags=creation).stdout
        return set(re.findall(r"^\s*[VAS][\w.]{5}\s+(\S+)", out, re.M))
    except Exception:
        return set()


AUDIO_NEEDS_TRANSCODE_MP4 = {"pcm_s16le", "pcm_s24le", "pcm_f32le", "opus",
                             "vorbis", "flac", "dts", "truehd", "wmav2"}


def pick_video_encoder(available):
    for name in ("libx264", "h264_nvenc", "h264_qsv", "libopenh264", "mpeg4"):
        if name in available:
            return name
    return "mpeg4"


def build_ffmpeg_cmd(ff, src, dst, delta_deg, encoder, audio_codec, duration):
    """按 ffmpeg display_rotation 约定（负=顺时针）生成滤镜链。"""
    d = delta_deg % 360
    if d == 270:            # 顺时针 90°
        vf = "transpose=1"
    elif d == 90:           # 逆时针 90°
        vf = "transpose=2"
    elif d == 180:
        vf = "hflip,vflip"
    else:
        raise ValueError("无效旋转角度 %d" % delta_deg)

    ext = os.path.splitext(dst)[1].lower()
    cmd = [ff, "-hide_banner", "-y", "-i", src,
           "-vf", vf, "-map", "0:v:0", "-map", "0:a?",
           "-c:v", encoder]
    if encoder == "libx264":
        cmd += ["-preset", "medium", "-crf", "18"]
    elif encoder == "h264_nvenc":
        cmd += ["-preset", "p5", "-cq", "20"]
    elif encoder == "mpeg4":
        cmd += ["-q:v", "2"]
    cmd += ["-pix_fmt", "yuv420p"]

    if audio_codec and audio_codec.lower() in AUDIO_NEEDS_TRANSCODE_MP4 and ext in (".mp4", ".mov", ".m4v"):
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-c:a", "copy"]

    if ext in (".mp4", ".mov", ".m4v"):
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", dst]
    return cmd


def probe_with_ffprobe(fp, src):
    """有 ffprobe 时获取精确的时长/分辨率/旋转/音频编码。"""
    if not fp:
        return None
    try:
        creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        out = subprocess.run(
            [fp, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,codec_name:stream_side_data=rotation:format=duration",
             "-of", "json", src],
            capture_output=True, text=True, errors="replace", timeout=20,
            creationflags=creation).stdout
        data = json.loads(out or "{}")
        st = (data.get("streams") or [{}])[0]
        rot = 0
        for sd in st.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = float(sd["rotation"])
        dur = None
        try:
            dur = float(data.get("format", {}).get("duration"))
        except (TypeError, ValueError):
            pass
        w, h = int(st.get("width") or 0), int(st.get("height") or 0)
        if abs(rot) % 180 == 90:
            w, h = h, w
        # 音频编码单独查一次
        acodec = None
        out2 = subprocess.run(
            [fp, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", src],
            capture_output=True, text=True, errors="replace", timeout=20,
            creationflags=creation).stdout.strip()
        if out2:
            acodec = out2.splitlines()[0]
        return {"width": w, "height": h, "duration": dur, "audio_codec": acodec}
    except Exception:
        return None


def probe_with_ffmpeg(ff, src):
    """没有 ffprobe 时，用 ffmpeg -i 的 stderr 兜底解析分辨率/时长。"""
    if not ff:
        return None
    try:
        creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        r = subprocess.run([ff, "-hide_banner", "-i", src], capture_output=True,
                           text=True, errors="replace", timeout=25,
                           creationflags=creation)
        txt = r.stderr or ""
        dur = None
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", txt)
        if m:
            dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        w = h = 0
        rot = 0
        m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", txt)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
        m = re.search(r"rotate\s*:\s*(-?\d+)", txt)
        if m:
            rot = int(m.group(1))
        if abs(rot) % 180 == 90:
            w, h = h, w
        acodec = None
        m = re.search(r"Audio:\s*([\w\-]+)", txt)
        if m:
            acodec = m.group(1)
        if w:
            return {"width": w, "height": h, "duration": dur,
                    "rotation": rot, "audio_codec": acodec}
    except Exception:
        pass
    return None


THUMB_H = 400


def extract_thumb(ff, src, delta, dst_png, seek=0.5):
    """
    用 ffmpeg 截一帧存成 PNG。delta != 0 时连同画面一起旋转，
    这样「结果」缩略图就是真实旋转后的样子。返回 True/False。
    """
    if not ff:
        return False
    d = delta % 360
    chain = []
    if d == 270:
        chain.append("transpose=1")        # 顺时针 90°
    elif d == 90:
        chain.append("transpose=2")        # 逆时针 90°
    elif d == 180:
        chain.append("hflip,vflip")
    chain.append("scale=-2:%d" % THUMB_H)
    cmd = [ff, "-hide_banner", "-y", "-loglevel", "error",
           "-ss", "%.2f" % max(0.0, seek), "-i", src,
           "-frames:v", "1", "-an", "-sn",
           "-vf", ",".join(chain), "-q:v", "3", dst_png]
    creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30,
                           creationflags=creation)
        return r.returncode == 0 and os.path.getsize(dst_png) > 0
    except Exception:
        return False


class WinDropTarget(object):
    """
    Windows 原生文件拖放（纯 ctypes，不依赖 tkinterdnd2）：
    DragAcceptFiles + 子类化窗口过程，接管 WM_DROPFILES。

    注意：窗口过程/返回值一律用 c_ssize_t 取，不要用 c_void_p ——
    ctypes 会把指针值 0 转成 None，回调返回 None 时会抛
    "NoneType object cannot be interpreted as an integer"。
    """

    WM_DROPFILES = 0x0233
    GWLP_WNDPROC = -4

    def __init__(self, root, sink):
        self.ok = False
        self._refs = []
        self._old = 0
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            sh = ctypes.windll.shell32
            self._ct, self._u, self._sh = ctypes, u, sh
            self._sink = sink          # 只往列表里塞路径，不回调 Tk

            hwnd_val = 0
            try:
                hwnd_val = int(str(root.frame()), 16)
            except Exception:
                hwnd_val = 0
            if not hwnd_val:
                hwnd_val = root.winfo_id()
            self._hwnd = wintypes.HWND(hwnd_val)

            u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
            u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                            ctypes.c_void_p]
            u.CallWindowProcW.restype = ctypes.c_ssize_t
            u.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                          wintypes.UINT, wintypes.WPARAM,
                                          wintypes.LPARAM]

            old = u.GetWindowLongPtrW(self._hwnd, self.GWLP_WNDPROC)
            if not old:
                return

            proto = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND,
                                       wintypes.UINT, wintypes.WPARAM,
                                       wintypes.LPARAM)

            def _wndproc(hwnd, msg, wparam, lparam):
                if msg == 0x0233:                     # WM_DROPFILES
                    # 关键：这里处于 Tcl 事件派发之中，绝不能调用 tkinter，
                    # 否则会重入 Tcl 导致 PyEval_RestoreThread 崩溃。
                    try:
                        self._sink.extend(self._query(wparam))
                    except Exception:
                        pass
                    try:
                        sh.DragFinish(ctypes.c_void_p(wparam))
                    except Exception:
                        pass
                    return 0
                try:
                    return int(u.CallWindowProcW(self._old, hwnd, msg,
                                                 wparam, lparam) or 0)
                except Exception:
                    return 0

            cb = proto(_wndproc)
            self._refs.append(cb)
            self._cb = cb
            self._old = old
            u.SetWindowLongPtrW(self._hwnd, self.GWLP_WNDPROC,
                                ctypes.cast(cb, ctypes.c_void_p))
            sh.DragAcceptFiles(self._hwnd, True)
            self.ok = True
        except Exception:
            self.ok = False
            self._old = 0

    def _query(self, hdrop):
        ct = self._ct
        from ctypes import wintypes
        fn = self._sh.DragQueryFileW
        fn.restype = wintypes.UINT
        fn.argtypes = [ct.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        h = ct.c_void_p(hdrop)
        n = fn(h, 0xFFFFFFFF, None, 0)
        out = []
        for i in range(n):
            ln = fn(h, i, None, 0)
            if ln <= 0:
                continue
            buf = ct.create_unicode_buffer(ln + 1)
            fn(h, i, buf, ln + 1)
            if buf.value:
                out.append(buf.value)
        return out


def run_ffmpeg(cmd, duration, on_progress, on_line):
    """执行 ffmpeg，解析 -progress 输出。返回 (ok, message)。"""
    creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace", bufsize=1,
                            creationflags=creation)
    tail = []

    def read_err():
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                tail.append(line)
                if len(tail) > 40:
                    del tail[0]
                on_line(line)

    t = threading.Thread(target=read_err, daemon=True)
    t.start()

    speed = ""
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_us=") and duration:
            try:
                us = int(line.split("=", 1)[1])
                on_progress(min(0.999, us / 1e6 / duration), speed)
            except ValueError:
                pass
        elif line.startswith("speed="):
            speed = line.split("=", 1)[1]
        elif line == "progress=end":
            on_progress(1.0, speed)
    proc.wait()
    t.join(timeout=2)
    if proc.returncode == 0:
        return True, ""
    return False, "\n".join(tail[-8:])


# ======================================================================
#  三、工具函数
# ======================================================================
def human_size(n):
    if n < 1024:
        return "%d B" % n
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return "%.1f %s" % (n, unit)
    return "%.1f PB" % (n / 1024.0)


def human_dur(sec):
    if not sec or sec <= 0:
        return "—"
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


def angle_label_short(delta):
    """预览箭头用的短标注，避免挤到图框。"""
    d = delta % 360
    return {270: "顺 90°", 90: "逆 90°", 180: "180°"}.get(d, "%d°" % d if d else "—")


def angle_label(delta):
    """把累计角度转成中文描述。"""
    d = delta % 360
    if d == 0:
        return "—"
    if d == 270:
        return "顺时针 90°"
    if d == 90:
        return "逆时针 90°"
    if d == 180:
        return "180°"
    return "%d°" % d


def unique_path(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while os.path.exists("%s (%d)%s" % (stem, i, ext)):
        i += 1
    return "%s (%d)%s" % (stem, i, ext)


# ======================================================================
#  四、图形界面
# ======================================================================
def _enable_dpi():
    if os.name != "nt":
        return
    try:
        from ctypes import windll
        try:
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _pick_font(root, candidates, size):
    try:
        import tkinter.font as tkfont
        fams = set(tkfont.families(root))
    except Exception:
        fams = set()
    for name in candidates:
        if name in fams:
            return (name, size)
    return (candidates[-1], size)


class VideoRotatorApp(object):
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.root = root
        self.items = []          # 每个元素是一个 dict
        self.busy = False
        self._job_queue = queue.Queue()
        self._ui_queue = queue.Queue()
        # 缩略图缓存：key -> PhotoImage（False 表示该帧取不到）
        self._thumbs = {}
        self._thumb_pending = set()
        self._drop_sink = []
        self._thumb_dir = None
        self._cancel = threading.Event()

        self.cfg = self._load_cfg()
        self.ffmpeg = find_binary("ffmpeg", self.cfg.get("ffmpeg"))
        self.ffprobe = find_binary("ffprobe", self.cfg.get("ffprobe"))
        if not self.ffprobe and self.ffmpeg:
            sib = os.path.join(os.path.dirname(self.ffmpeg), "ffprobe.exe")
            if os.path.isfile(sib):
                self.ffprobe = sib
        self.ff_ver = ffmpeg_version(self.ffmpeg)
        self.encoders = list_encoders(self.ffmpeg) if self.ffmpeg else set()

        self._build_fonts()
        self._build_style()
        self._build_ui()
        self._refresh_ffmpeg_badge()
        self._init_drop()
        self.root.after(60, self._fit_right)
        self._poll_id = self.root.after(80, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # 兜底：无论从哪条路径销毁窗口，都先取消排队中的轮询，
        # 否则 Tcl 会尝试执行已注销的 Python 命令并打印
        # invalid command name "..._poll"。
        self.root.bind("<Destroy>", self._on_destroy, add="+")

    # ------------------------------------------------------------ 配置
    def _cfg_path(self):
        return os.path.join(script_dir(), CONFIG_NAME)

    def _load_cfg(self):
        try:
            with open(self._cfg_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_cfg(self):
        try:
            with open(self._cfg_path(), "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ------------------------------------------------------------ 外观
    def _build_fonts(self):
        tk = self.tk
        self.f_base = _pick_font(self.root, ["Microsoft YaHei UI", "微软雅黑", "Segoe UI"], 9)
        self.f_bold = (self.f_base[0], 9, "bold")
        self.f_title = (self.f_base[0], 13, "bold")
        self.f_small = (self.f_base[0], 8)
        self.f_mono = _pick_font(self.root, ["Consolas", "Cascadia Mono", "Courier New"], 9)

    def _build_style(self):
        tk = self.tk
        s = self.ttk.Style(self.root)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure(".", background=BG, foreground=FG, font=self.f_base)
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=CARD)
        s.configure("TLabel", background=BG, foreground=FG, font=self.f_base)
        s.configure("Card.TLabel", background=CARD, foreground=FG, font=self.f_base)
        s.configure("Muted.TLabel", background=BG, foreground=MUTED, font=self.f_small)
        s.configure("MutedCard.TLabel", background=CARD, foreground=MUTED, font=self.f_small)
        s.configure("Title.TLabel", background=BG, foreground=FG, font=self.f_title)
        s.configure("TCheckbutton", background=BG, foreground=FG, font=self.f_base)
        s.map("TCheckbutton", background=[("active", BG)], foreground=[("active", FG)])
        s.configure("TRadiobutton", background=BG, foreground=FG, font=self.f_base)
        s.map("TRadiobutton", background=[("active", BG)], foreground=[("active", FG)])
        s.configure("TProgressbar", background=ACCENT, troughcolor=CARD2,
                    bordercolor=CARD2, lightcolor=ACCENT, darkcolor=ACCENT, thickness=8)
        s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=FG,
                    borderwidth=0, rowheight=24, font=self.f_base)
        s.configure("Treeview.Heading", background=CARD2, foreground=MUTED,
                    font=self.f_small, relief="flat", borderwidth=0)
        s.map("Treeview.Heading", background=[("active", LINE)])
        s.map("Treeview", background=[("selected", "#2b4a8a")],
              foreground=[("selected", "#ffffff")])
        s.configure("TCombobox", fieldbackground=CARD2, background=CARD2,
                    foreground=FG, arrowcolor=MUTED, borderwidth=0)
        s.map("TCombobox", fieldbackground=[("readonly", CARD2)],
              foreground=[("readonly", FG)])
        s.configure("TEntry", fieldbackground=CARD2, foreground=FG,
                    insertcolor=FG, borderwidth=0)
        s.configure("TSeparator", background=LINE)
        self.root.option_add("*TCombobox*Listbox.background", CARD2)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

    def _btn(self, parent, text, cmd, kind="ghost", width=None, font=None):
        """自绘扁平按钮（tk.Button 便于完全控制暗色配色）。"""
        colors = {
            "primary": (ACCENT, "#ffffff", ACCENT_HI),
            "ghost": (CARD2, FG, LINE),
            "danger": ("#3a2224", "#ff8a80", "#4a2a2d"),
        }[kind]
        b = self.tk.Button(parent, text=text, command=cmd, bd=0, relief="flat",
                           bg=colors[0], fg=colors[1], activebackground=colors[2],
                           activeforeground=colors[1], font=font or self.f_base,
                           cursor="hand2", padx=12, pady=5,
                           highlightthickness=0, takefocus=0)
        if width:
            b.configure(width=width)
        b.bind("<Enter>", lambda e: b.configure(bg=colors[2]))
        b.bind("<Leave>", lambda e: b.configure(bg=colors[0]))
        return b

    def _build_ui(self):
        tk = self.tk
        root = self.root
        root.title("%s v%s" % (APP_NAME, APP_VER))
        root.configure(bg=BG)
        root.geometry("1060x780")
        root.minsize(940, 700)

        pad = {"padx": 14}

        # ---------- 顶栏 ----------
        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", pady=(12, 6), **pad)
        tk.Label(head, text="🎬 " + APP_NAME, bg=BG, fg=FG,
                 font=(self.f_base[0], 15, "bold")).pack(side="left")
        tk.Label(head, text="v%s" % APP_VER, bg=BG, fg=MUTED,
                 font=self.f_small).pack(side="left", padx=(6, 0), pady=(6, 0))
        self.badge = tk.Label(head, text="", bg=CARD2, fg=MUTED, font=self.f_small,
                              padx=9, pady=3)
        self.badge.pack(side="right")
        tk.Label(head, text="内置 ffmpeg · 离线可用", bg=BG, fg=CYAN,
                 font=self.f_small).pack(side="right", padx=(0, 10))

        # ---------- 工具栏 ----------
        bar = tk.Frame(root, bg=BG)
        bar.pack(fill="x", pady=(0, 8), **pad)
        self._btn(bar, "＋ 添加视频", self.add_files, "primary").pack(side="left")
        self._btn(bar, "＋ 添加文件夹", self.add_folder).pack(side="left", padx=6)
        self._btn(bar, "移除所选", self.remove_selected).pack(side="left")
        self._btn(bar, "清空", self.clear_all).pack(side="left", padx=6)
        tk.Label(bar, text="（可直接把视频/文件夹拖进窗口 · Ctrl 多选）", bg=BG,
                 fg=MUTED, font=self.f_small).pack(side="left", padx=8)

        # ---------- 输出位置（通栏，避免右栏内容超高） ----------
        outrow = tk.Frame(root, bg=BG)
        outrow.pack(fill="x", pady=(0, 8), **pad)
        tk.Label(outrow, text="输出位置", bg=BG, fg=MUTED,
                 font=self.f_small).pack(side="left", padx=(0, 8))
        self.outdir = tk.StringVar(value=self.cfg.get("outdir", ""))
        e = tk.Entry(outrow, textvariable=self.outdir, bg=CARD2, fg=FG, bd=0,
                     insertbackground=FG, font=self.f_base, relief="flat")
        e.pack(side="left", fill="x", expand=True, ipady=5)
        self._btn(outrow, "浏览", self.pick_outdir).pack(side="left", padx=(6, 0))
        self._btn(outrow, "打开输出目录", self.open_outdir).pack(side="left", padx=(6, 0))
        tk.Label(outrow, text="留空 = 源文件目录下新建 rotated\\", bg=BG, fg=MUTED,
                 font=self.f_small).pack(side="left", padx=(8, 0))

        # ---------- 主体：左列表 + 右预览 ----------
        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, **pad)

        left = tk.Frame(body, bg=CARD, highlightthickness=1,
                        highlightbackground=LINE)

        cols = ("idx", "name", "res", "dur", "size", "plan", "status")
        self.tree = self.ttk.Treeview(left, columns=cols, show="headings",
                                         selectmode="extended")
        self.tree.heading("idx", text="#")
        self.tree.heading("name", text="文件名")
        self.tree.heading("res", text="分辨率")
        self.tree.heading("dur", text="时长")
        self.tree.heading("size", text="大小")
        self.tree.heading("plan", text="旋转操作")
        self.tree.heading("status", text="状态")
        self.tree.column("idx", width=34, anchor="center", stretch=False)
        self.tree.column("name", width=230, anchor="w")
        self.tree.column("res", width=98, anchor="center", stretch=False)
        self.tree.column("dur", width=60, anchor="center", stretch=False)
        self.tree.column("size", width=74, anchor="e", stretch=False)
        self.tree.column("plan", width=88, anchor="center", stretch=False)
        self.tree.column("status", width=72, anchor="center", stretch=False)

        vsb = self.ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(2, 0), pady=2)
        vsb.pack(side="right", fill="y", pady=2, padx=(0, 2))

        self.tree.tag_configure("done", foreground=OK)
        self.tree.tag_configure("fail", foreground=ERR)
        self.tree.tag_configure("run", foreground=CYAN)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_preview())
        self.tree.bind("<Double-1>", lambda e: self.open_source_file())
        self.tree.bind("<Button-3>", self._tree_menu)

        # 右栏
        right = tk.Frame(body, bg=BG, width=300)
        right.pack(side="right", fill="y", padx=(12, 0))
        right.pack_propagate(False)
        self._right = right
        # 右栏（固定宽）先占位，左栏再吃掉剩余宽度，避免表格把右栏挤窄
        left.pack(side="left", fill="both", expand=True)

        pv = tk.Frame(right, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        pv.pack(fill="x")
        self._pv = pv
        tk.Label(pv, text="旋转预览", bg=CARD, fg=MUTED, font=self.f_small,
                 anchor="w").pack(fill="x", padx=10, pady=(6, 0))
        self.canvas = tk.Canvas(pv, bg=CARD, highlightthickness=0, height=138)
        self.canvas.pack(fill="x", padx=8, pady=(2, 8))
        self.canvas.bind("<Configure>", lambda e: self._update_preview())

        # 操作面板
        ops = tk.Frame(right, bg=BG)
        ops.pack(fill="x", pady=(8, 0))
        tk.Label(ops, text="旋转操作（选中项；未选=全部）", bg=BG, fg=MUTED,
                 font=self.f_small, anchor="w").pack(fill="x", pady=(0, 4))
        row1 = tk.Frame(ops, bg=BG)
        row1.pack(fill="x")
        self._btn(row1, "↻ 顺时针 90°", lambda: self.mark(-90), "primary",
                  font=self.f_bold).pack(side="left", fill="x", expand=True)
        self._btn(row1, "↺ 逆时针 90°", lambda: self.mark(90), "primary",
                  font=self.f_bold).pack(side="left", fill="x", expand=True, padx=(6, 0))
        row2 = tk.Frame(ops, bg=BG)
        row2.pack(fill="x", pady=(4, 0))
        self._btn(row2, "旋转 180°", lambda: self.mark(180)).pack(side="left", fill="x", expand=True)
        self._btn(row2, "撤销标记", lambda: self.mark(0, reset=True)).pack(side="left", fill="x", expand=True, padx=(6, 0))

        # 引擎选择
        eng = tk.Frame(right, bg=BG)
        eng.pack(fill="x", pady=(10, 0))
        tk.Label(eng, text="处理引擎", bg=BG, fg=MUTED, font=self.f_small,
                 anchor="w").pack(fill="x", pady=(0, 4))
        self.mode = tk.StringVar(value=self.cfg.get("mode", "auto"))
        for val, txt in (("auto", "自动选择（推荐）"),
                         ("meta", "仅元数据 · 秒完无损"),
                         ("ffmpeg", "仅 ffmpeg 重编码")):

            tk.Radiobutton(eng, text=txt, value=val, variable=self.mode, bg=BG, fg=FG,
                           activebackground=BG, activeforeground=FG, selectcolor=CARD2,
                           font=self.f_small, anchor="w", bd=0, highlightthickness=0,
                           cursor="hand2").pack(fill="x")
        self.mode_hint = tk.Label(eng, text="", bg=BG, fg=MUTED, font=self.f_small,
                                  anchor="w", justify="left", wraplength=262)
        self.mode_hint.pack(fill="x", pady=(4, 0))
        self.mode.trace_add("write", lambda *a: self._on_mode_change())
        right.bind("<Configure>", lambda e: self._fit_right())

        # ---------- 底部：开始 + 进度 + 日志 ----------
        bot = tk.Frame(root, bg=BG)
        bot.pack(fill="both", **pad, pady=(8, 12))

        runrow = tk.Frame(bot, bg=BG)
        runrow.pack(fill="x")
        self.btn_run = self._btn(runrow, "▶  开始处理", self.start, "primary",
                                 font=(self.f_base[0], 10, "bold"))
        self.btn_run.pack(side="left", ipadx=10, ipady=3)
        self.btn_stop = self._btn(runrow, "■ 停止", self.stop, "danger")
        self.btn_stop.pack(side="left", padx=(8, 0), ipady=1)
        self.btn_stop.configure(state="disabled")
        self.prog_text = tk.Label(runrow, text="", bg=BG, fg=MUTED, font=self.f_small,
                                  anchor="w")
        self.prog_text.pack(side="left", padx=12)
        self.prog = self.ttk.Progressbar(runrow, mode="determinate", maximum=1000)
        self.prog.pack(side="right", fill="x", expand=True, padx=(12, 0), ipady=1)

        logbox = tk.Frame(bot, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        logbox.pack(fill="both", expand=True, pady=(10, 0))
        head2 = tk.Frame(logbox, bg=CARD)
        head2.pack(fill="x", padx=8, pady=(6, 0))
        tk.Label(head2, text="日志", bg=CARD, fg=MUTED, font=self.f_small).pack(side="left")
        tk.Label(head2, text="右键可复制", bg=CARD, fg=LINE, font=self.f_small).pack(side="right")
        self.log = tk.Text(logbox, bg=CARD, fg=FG, bd=0, height=7, wrap="word",
                           insertbackground=FG, font=self.f_mono, padx=8, pady=6,
                           relief="flat", highlightthickness=0)
        lsb = self.ttk.Scrollbar(logbox, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set, state="disabled")
        self.log.pack(side="left", fill="both", expand=True, padx=(0, 0), pady=(0, 4))
        lsb.pack(side="right", fill="y", pady=(0, 4), padx=(0, 2))
        self.log.tag_configure("ok", foreground=OK)
        self.log.tag_configure("warn", foreground=WARN)
        self.log.tag_configure("err", foreground=ERR)
        self.log.tag_configure("info", foreground=CYAN)
        self.log.tag_configure("dim", foreground=MUTED)
        self.log.bind("<Button-3>", self._log_menu)
        self.log.bind("<Control-c>", lambda e: (self._log_copy(), "break")[1])
        self.log.bind("<Control-C>", lambda e: (self._log_copy(), "break")[1])

        # 状态栏
        st = tk.Frame(root, bg=CARD2)
        st.pack(fill="x", side="bottom")
        self.status = tk.Label(st, text="就绪", bg=CARD2, fg=MUTED, font=self.f_small,
                               anchor="w", padx=10, pady=3)
        self.status.pack(side="left")
        self.count_lbl = tk.Label(st, text="0 个文件", bg=CARD2, fg=MUTED,
                                  font=self.f_small, padx=10)
        self.count_lbl.pack(side="right")

        self._on_mode_change()
        self._log("就绪。添加视频 → 点旋转按钮标记 → 开始处理。", "dim")
        self._log("元数据引擎不会重新编码，画质零损失；若播放器不识别旋转，请改用 ffmpeg 重编码。", "dim")

        # 快捷键
        root.bind("<Control-o>", lambda e: self.add_files())
        root.bind("<Control-O>", lambda e: self.add_files())
        root.bind("<F5>", lambda e: self.start())
        root.bind("<Delete>", self._hk_delete)
        root.bind("<Control-a>", self._hk_select_all)

    def _in_text_input(self, widget):
        return isinstance(widget, (self.tk.Entry, self.tk.Text))

    def _hk_delete(self, event):
        if self._in_text_input(event.widget):
            return None
        self.remove_selected()
        return "break"

    def _hk_select_all(self, event):
        if self._in_text_input(event.widget):
            return None
        self._select_all()
        return "break"

    # ------------------------------------------------------------ 右键菜单
    def _tree_menu(self, event):
        tk = self.tk
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        m = tk.Menu(self.root, tearoff=0, bg=CARD2, fg=FG, bd=0,
                    activebackground=ACCENT, activeforeground="white",
                    font=self.f_small)
        m.add_command(label="打开源文件（默认播放器）", command=self.open_source_file)
        m.add_command(label="打开所在文件夹", command=self.open_source_dir)
        m.add_separator()
        m.add_command(label="顺时针 90°", command=lambda: self.mark(-90))
        m.add_command(label="逆时针 90°", command=lambda: self.mark(90))
        m.add_command(label="撤销标记", command=lambda: self.mark(0, reset=True))
        m.add_separator()
        m.add_command(label="移除所选", command=self.remove_selected)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _log_menu(self, event):
        tk = self.tk
        m = tk.Menu(self.root, tearoff=0, bg=CARD2, fg=FG, bd=0,
                    activebackground=ACCENT, activeforeground="white",
                    font=self.f_small)
        m.add_command(label="复制", command=self._log_copy)
        m.add_command(label="全选", command=lambda: self._log_sel())
        m.add_command(label="清空", command=lambda: self._log_clear())
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _log_copy(self):
        try:
            txt = self.log.get("sel.first", "sel.last")
        except Exception:
            txt = self.log.get("1.0", "end-1c")
        if txt:
            self.root.clipboard_clear()
            self.root.clipboard_append(txt)

    def _log_sel(self):
        self.log.tag_add("sel", "1.0", "end-1c")

    def _log_clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _log(self, msg, tag=None):
        self.log.configure(state="normal")
        ts = time.strftime("%H:%M:%S")
        self.log.insert("end", "[%s] " % ts, ("dim",))
        self.log.insert("end", msg + "\n", (tag,) if tag else ())
        self.log.see("end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------ 文件列表
    def add_files(self):
        types = [("视频文件", " ".join("*" + e for e in sorted(VIDEO_EXTS))),
                 ("所有文件", "*.*")]
        paths = self.filedialog.askopenfilenames(
            title="选择视频", filetypes=types,
            initialdir=self.cfg.get("last_dir") or os.path.expanduser("~"))
        if not paths:
            return
        self.cfg["last_dir"] = os.path.dirname(paths[0])
        self._save_cfg()
        self._add_paths(list(paths))

    def add_folder(self):
        d = self.filedialog.askdirectory(
            title="选择文件夹（含子目录）",
            initialdir=self.cfg.get("last_dir") or os.path.expanduser("~"))
        if not d:
            return
        self.cfg["last_dir"] = d
        self._save_cfg()
        found = []
        for root, _dirs, files in os.walk(d):
            for fn in sorted(files):
                if os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                    found.append(os.path.join(root, fn))
        if not found:
            self._log("该文件夹内没有找到视频文件", "warn")
            return
        self._add_paths(found)

    def _add_paths(self, paths):
        exist = {it["path"] for it in self.items}
        added = 0
        for p in paths:
            p = os.path.abspath(p)
            if p in exist:
                continue
            if os.path.splitext(p)[1].lower() not in VIDEO_EXTS:
                self._log("跳过非视频文件：%s" % os.path.basename(p), "dim")
                continue
            it = {"path": p, "name": os.path.basename(p), "size": 0,
                  "res": "—", "dur": None, "meta_rot": 0.0,
                  "delta": 0, "status": "等待", "tag": "", "mp4": False}
            try:
                it["size"] = os.path.getsize(p)
            except OSError:
                pass
            it["mp4"] = os.path.splitext(p)[1].lower() in MP4_EXTS
            self._read_info(it)
            self.items.append(it)
            exist.add(p)
            added += 1
        self._rebuild_tree()
        self._log("已添加 %d 个文件（共 %d 个）" % (added, len(self.items)), "info")
        self.count_lbl.configure(text="%d 个文件" % len(self.items))

    def _read_info(self, it):
        p = it["path"]
        if it["mp4"]:
            m = read_mp4_meta(p)
            if m:
                rot = m["rotation"] or 0.0
                w, h = m["width"], m["height"]
                if abs(round(rot / 90)) % 2 == 1:
                    w, h = h, w
                it["res"] = "%d×%d" % (w, h)
                it["dur"] = m["duration"]
                it["meta_rot"] = rot
                it["audio_codec"] = None
                return
        pr = probe_with_ffprobe(self.ffprobe, p) or probe_with_ffmpeg(self.ffmpeg, p)
        if pr and pr.get("width"):
            it["res"] = "%d×%d" % (pr["width"], pr["height"])
            it["dur"] = pr.get("duration")
            # ffmpeg/ffprobe 的 rotation 与内置矩阵读取现在是同一套语义
            # （正 = 逆时针），不要再取反，否则提示文字方向会反。
            it["meta_rot"] = pr.get("rotation") or 0
            it["audio_codec"] = pr.get("audio_codec")
        else:
            it.setdefault("audio_codec", None)

    def _rebuild_tree(self):
        sel = set(self.tree.selection())
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for i, it in enumerate(self.items):
            iid = str(i)
            self.tree.insert("", "end", iid=iid, values=(
                i + 1, it["name"], it["res"], human_dur(it["dur"]),
                human_size(it["size"]), angle_label(it["delta"]), it["status"]),
                tags=(it["tag"],) if it["tag"] else ())
            if iid in sel:
                self.tree.selection_add(iid)
        self._update_preview()

    def _update_row(self, idx):
        it = self.items[idx]
        iid = str(idx)
        if self.tree.exists(iid):
            self.tree.item(iid, values=(
                idx + 1, it["name"], it["res"], human_dur(it["dur"]),
                human_size(it["size"]), angle_label(it["delta"]), it["status"]),
                tags=(it["tag"],) if it["tag"] else ())

    def _select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def _selected_indices(self):
        return [int(i) for i in self.tree.selection()]

    def remove_selected(self):
        if self.busy:
            return
        idxs = sorted(self._selected_indices(), reverse=True)
        if not idxs:
            return
        for i in idxs:
            del self.items[i]
        self._rebuild_tree()
        self.count_lbl.configure(text="%d 个文件" % len(self.items))

    def clear_all(self):
        if self.busy:
            return
        self.items = []
        self._rebuild_tree()
        self.count_lbl.configure(text="0 个文件")

    # ------------------------------------------------------------ 标记旋转
    def mark(self, delta, reset=False):
        if self.busy:
            self._log("处理中，请稍候或先停止", "warn")
            return
        idxs = self._selected_indices()
        if not idxs:
            idxs = list(range(len(self.items)))
        if not idxs:
            self._log("请先添加视频", "warn")
            return
        for i in idxs:
            it = self.items[i]
            it["delta"] = 0 if reset else (it["delta"] + delta) % 360
            it["status"] = "等待"
            it["tag"] = ""
        # 角度归一化到 [-180, 180] 便于显示
        for i in idxs:
            d = self.items[i]["delta"]
            if d > 180:
                self.items[i]["delta"] = d - 360
        self._rebuild_tree()
        tail = "撤销全部标记" if reset else angle_label(delta)
        self._log("已对 %d 个文件应用：%s" % (len(idxs), tail), "info")

    def _fit_right(self):
        """
        按右栏实际高度自适应预览画布高度：
        除画布以外的所有内容高度是固定的，剩余高度全部给画布，
        这样高 DPI / 小窗口下底部控件也不会被裁掉。
        """
        avail = self._right.winfo_height()
        if avail <= 1:
            return
        canvas_now = int(self.canvas.cget("height"))
        total_req = sum(ch.winfo_reqheight() for ch in self._right.winfo_children())
        gap = 8 + 10                     # ops / eng 两个子框架的 pady
        h = avail - (total_req - canvas_now) - gap
        h = max(56, min(180, h))
        if abs(h - canvas_now) > 2:
            self.canvas.configure(height=h)

    MODE_HINT = {
        "auto": "MP4/MOV 改写显示矩阵，秒完无损；其它容器转 ffmpeg。",
        "meta": "全部改写矩阵不重编码，仅支持 MP4/MOV/M4V/3GP。",
        "ffmpeg": "全部像素级重编码，任何播放器都认，需 ffmpeg.exe。",
    }

    def _on_mode_change(self):
        self.cfg["mode"] = self.mode.get()
        self._save_cfg()
        if hasattr(self, "mode_hint"):
            self.mode_hint.configure(text=self.MODE_HINT.get(self.mode.get(), ""))
        if hasattr(self, "_right"):
            self.root.after(10, self._fit_right)

    # ------------------------------------------------------------ 预览
    def _update_preview(self):
        """在 Canvas 上画「源 → 结果」的比例示意，完全按画布实际宽度自适应。"""
        c = self.canvas
        c.delete("all")
        cw = c.winfo_width()
        if cw <= 1:
            cw = 280
        ch = int(c.winfo_height() or 168)
        idxs = self._selected_indices()
        if not idxs:
            c.create_text(cw / 2, ch / 2, text="选择文件查看旋转预览",
                          fill=MUTED, font=self.f_small)
            return
        it = self.items[idxs[0]]
        w, h = 1920, 1080
        try:
            rw, rh = it["res"].split("×")
            w, h = int(rw), int(rh)
        except Exception:
            pass

        def fit(bw, bh, mw, mh):
            sc = min(mw / float(bw), mh / float(bh))
            return bw * sc, bh * sc

        d = it["delta"]
        ow, oh = (h, w) if (d % 180) == 90 else (w, h)

        top = 26
        maxbox_h = ch - top - 34
        slot_w = (cw - 46) / 2.0            # 左右各占一半，中间留箭头位
        cx1 = slot_w / 2.0 + 4
        cx2 = cw - slot_w / 2.0 - 4
        maxbox_w = slot_w - 12

        # 源
        c.create_text(cx1, 13, text="源", fill=MUTED, font=self.f_small)
        sw, sh = fit(w, h, maxbox_w, maxbox_h)
        c.create_rectangle(cx1 - sw / 2, top, cx1 + sw / 2, top + sh,
                           outline=MUTED, width=1)
        self._place_thumb(it, 0, cx1, top, sw, sh)
        c.create_text(cx1, top + sh + 11, text="%d×%d" % (w, h),
                      fill=MUTED, font=self.f_small)

        # 箭头
        mid_y = top + maxbox_h / 2.0
        c.create_line(cx1 + sw / 2 + 5, mid_y, cx2 - maxbox_w / 2 - 5, mid_y,
                      fill=ACCENT if d else LINE, width=2, arrow="last")
        if d:
            c.create_text((cx1 + cx2) / 2.0, mid_y - 10, text=angle_label_short(d),
                          fill=ACCENT, font=self.f_small)

        # 结果
        c.create_text(cx2, 13, text="结果", fill=FG, font=self.f_small)
        nw, nh = fit(ow, oh, maxbox_w, maxbox_h)
        c.create_rectangle(cx2 - nw / 2, top, cx2 + nw / 2, top + nh,
                           outline=ACCENT if d else MUTED, width=2 if d else 1)
        self._place_thumb(it, d, cx2, top, nw, nh)
        c.create_text(cx2, top + nh + 11, text="%d×%d" % (ow, oh), fill=FG,
                      font=self.f_small)
        if it["meta_rot"]:
            c.create_text(cw / 2, ch - 9, text="文件自带旋转 %.0f°" % it["meta_rot"],
                          fill=WARN, font=self.f_small)

    # ------------------------------------------------------------ 画面缩略图
    def _thumb_key(self, it, delta):
        try:
            mt = int(os.path.getmtime(it["path"]))
        except OSError:
            mt = 0
        return ("t", it["path"], mt, delta % 360)

    def _place_thumb(self, it, delta, cx, top, bw, bh):
        """把真实画面贴到示意框里；没缓存就异步截一帧。"""
        c = self.canvas
        key = self._thumb_key(it, delta)
        img = self._thumbs.get(key)
        if img is False:
            c.create_text(cx, top + bh / 2.0, text="取帧失败", fill=LINE,
                          font=self.f_small)
            return
        if img is None:
            if not self.ffmpeg:
                c.create_text(cx, top + bh / 2.0, text="需 ffmpeg 截帧", fill=LINE,
                              font=self.f_small)
            else:
                c.create_text(cx, top + bh / 2.0, text="截取画面…", fill=LINE,
                              font=self.f_small)
                self._request_thumb(it, delta, key)
            return
        pw, ph = img.width(), img.height()
        if pw < 2 or ph < 2:
            return
        k = max(1, int(math.ceil(max(pw / max(bw, 1.0), ph / max(bh, 1.0)))))
        skey = (key, k)
        small = self._thumbs.get(skey)
        if small is None:
            small = img.subsample(k, k) if k > 1 else img
            self._thumbs[skey] = small
        c.create_image(cx, top + bh / 2.0, image=small)

    def _request_thumb(self, it, delta, key):
        if key in self._thumb_pending or not self.ffmpeg:
            return
        if self._thumb_dir is None:
            self._thumb_dir = tempfile.mkdtemp(prefix="vrot_thumb_")
        self._thumb_pending.add(key)
        ff = self.ffmpeg
        src = it["path"]
        dur = it.get("dur") or 0
        seek = min(1.0, dur / 2.0) if dur > 1.5 else 0.25
        tag = hashlib.md5(("%s|%s|%s" % (src, key[2], key[3])).encode("utf-8")).hexdigest()[:14]
        out = os.path.join(self._thumb_dir, "t_%s.png" % tag)

        def work():
            ok = extract_thumb(ff, src, key[3], out, seek)
            self._ui_queue.put(("thumb", (key, out if ok else None)))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ 拖放
    def _init_drop(self):
        try:
            self.drop = WinDropTarget(self.root, self._drop_sink)
        except Exception:
            self.drop = None
        if self.drop is not None and self.drop.ok:
            self._log("已启用拖放：把视频文件或文件夹直接拖进窗口即可添加", "ok")
        else:
            self._log("拖放未启用（可点「添加视频」），其它功能不受影响", "dim")

    def _on_drop(self, paths):
        vids, dirs = [], []
        for p in paths:
            if os.path.isdir(p):
                dirs.append(p)
            elif os.path.splitext(p)[1].lower() in VIDEO_EXTS:
                vids.append(p)
            else:
                self._log("已忽略非视频文件：%s" % os.path.basename(p), "dim")
        for d in dirs:
            found = []
            for base, _sub, files in os.walk(d):
                for fn in files:
                    if os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                        found.append(os.path.join(base, fn))
            self._log("文件夹「%s」内发现 %d 个视频" % (os.path.basename(d) or d, len(found)), "info")
            vids.extend(found)
        if vids:
            self._add_paths(vids)

    # ------------------------------------------------------------ ffmpeg
    def _refresh_ffmpeg_badge(self):
        if self.ffmpeg:
            self.badge.configure(text="ffmpeg %s" % (self.ff_ver or "已就绪"), fg=OK)
            self.badge.bind("<Button-1>", lambda e: self._log(
                "ffmpeg: %s\nffprobe: %s\n视频编码器: %s" % (
                    self.ffmpeg, self.ffprobe or "未找到",
                    pick_video_encoder(self.encoders)), "dim"))
        else:
            self.badge.configure(text="未找到 ffmpeg（点此设置）", fg=WARN)
            self.badge.bind("<Button-1>", lambda e: self.locate_ffmpeg())
        self._update_preview()

    def locate_ffmpeg(self):
        path = self.filedialog.askopenfilename(
            title="选择 ffmpeg.exe",
            filetypes=[("ffmpeg", "ffmpeg.exe"), ("可执行文件", "*.exe")])
        if not path:
            return
        self.cfg["ffmpeg"] = path
        fp = os.path.join(os.path.dirname(path), "ffprobe.exe")
        if os.path.isfile(fp):
            self.cfg["ffprobe"] = fp
        self._save_cfg()
        self.ffmpeg = path
        self.ffprobe = find_binary("ffprobe", self.cfg.get("ffprobe"))
        self.ff_ver = ffmpeg_version(self.ffmpeg)
        self.encoders = list_encoders(self.ffmpeg)
        self._refresh_ffmpeg_badge()
        self._log("已设置 ffmpeg：%s（%s）" % (path, self.ff_ver), "ok")

    # ------------------------------------------------------------ 打开位置
    def open_outdir(self):
        d = self._resolve_outdir_for(self.items[0]) if self.items else self.outdir.get()
        if d and os.path.isdir(d):
            self._open(d)
        else:
            self._log("输出目录还不存在，处理完成后会自动创建", "warn")

    def _open(self, path):
        try:
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            self._log("打开失败：%s" % e, "err")

    def open_source_file(self):
        idxs = self._selected_indices()
        if idxs:
            self._open(self.items[idxs[0]]["path"])

    def open_source_dir(self):
        idxs = self._selected_indices()
        if idxs:
            self._open(os.path.dirname(self.items[idxs[0]]["path"]))

    def pick_outdir(self):
        d = self.filedialog.askdirectory(title="选择输出目录")
        if d:
            self.outdir.set(d)
            self.cfg["outdir"] = d
            self._save_cfg()

    def _resolve_outdir_for(self, it, explicit=None):
        d = (explicit if explicit is not None else self.outdir.get()) or ""
        d = d.strip()
        if d:
            return d
        return os.path.join(os.path.dirname(it["path"]), "rotated")

    # ------------------------------------------------------------ 处理流程
    def start(self):
        if self.busy:
            return
        todo = [i for i, it in enumerate(self.items) if it["delta"] % 360 != 0]
        if not todo:
            self._log("没有待处理的任务：请先选中文件并点「顺时针/逆时针」标记", "warn")
            return
        if self.mode.get() in ("ffmpeg", "auto") and not self.ffmpeg:
            need = [i for i in todo if self.mode.get() == "ffmpeg"
                    or not self.items[i]["mp4"]]
            if need:
                self._log("缺少 ffmpeg，%d 个文件无法处理。点击右上角徽标可指定 ffmpeg.exe" % len(need), "err")
                if self.mode.get() == "ffmpeg":
                    return
        self.busy = True
        self._cancel.clear()
        self.btn_run.configure(state="disabled", bg=CARD2)
        self.btn_stop.configure(state="normal", bg="#7a2a2d")
        self.prog.configure(value=0)
        # 关键：GUI 变量必须在主线程读取，工作线程不得触碰 Tcl
        self._run_mode = self.mode.get()
        self._run_outdir = (self.outdir.get() or "").strip()
        self._log("—— 开始处理 %d 个文件 ——" % len(todo), "info")
        t = threading.Thread(target=self._worker,
                             args=(todo, self._run_mode, self._run_outdir),
                             daemon=True)
        t.start()

    def stop(self):
        if self.busy:
            self._cancel.set()
            self._log("已请求停止，等待当前文件结束…", "warn")

    def _worker(self, todo, mode, outdir):
        total = len(todo)
        ok_cnt = 0
        for n, idx in enumerate(todo):
            if self._cancel.is_set():
                self._ui_queue.put(("log", ("已停止，剩余 %d 个未处理" % (total - n), "warn")))
                break
            it = self.items[idx]
            base_frac = n / float(total)

            def set_status(s, tag):
                it["status"] = s
                it["tag"] = tag
                self._ui_queue.put(("row", idx))

            def prog_cb(frac, speed=""):
                overall = (base_frac + frac / float(total)) * 1000
                self._ui_queue.put(("prog", (overall, "%d/%d  %s %s" % (
                    n + 1, total, os.path.basename(it["path"])[:26], speed))))

            try:
                set_status("处理中", "run")
                self._ui_queue.put(("log", ("[%d/%d] %s  →  %s" % (
                    n + 1, total, it["name"], angle_label(it["delta"])), "info")))
                dst = self._process_one(it, prog_cb, mode, outdir)
                set_status("完成", "done")
                ok_cnt += 1
                self._ui_queue.put(("log", ("    ✔ 输出：%s" % dst, "ok")))
            except _Cancelled:
                set_status("已取消", "warn")
                break
            except Exception as e:
                set_status("失败", "fail")
                self._ui_queue.put(("log", ("    ✘ %s" % e, "err")))
            self._ui_queue.put(("prog", ((n + 1) / float(total) * 1000, "")))
        self._ui_queue.put(("done", (ok_cnt, total)))

    def _process_one(self, it, prog_cb, mode, outdir_root):
        src = it["path"]
        delta = it["delta"]
        stem, ext = os.path.splitext(it["name"])
        suffix = {270: "_cw90", 90: "_ccw90", 180: "_180"}.get(delta % 360, "_rot")
        outdir = outdir_root or os.path.join(os.path.dirname(src), "rotated")
        os.makedirs(outdir, exist_ok=True)

        use_meta = (mode == "meta") or (mode == "auto" and it["mp4"])
        if use_meta and not it["mp4"]:
            raise RuntimeError("元数据引擎仅支持 MP4/MOV/M4V，请改用 ffmpeg 重编码")

        dst = unique_path(os.path.join(outdir, stem + suffix + ext))

        if use_meta:
            if self._cancel.is_set():
                raise _Cancelled()
            b, a = metadata_rotate(src, dst, delta, log=lambda m, t: self._ui_queue.put(("log", (m, t))))
            prog_cb(1.0, "")
            return dst

        if not self.ffmpeg:
            raise RuntimeError("未找到 ffmpeg，无法重编码（可改用元数据引擎或指定 ffmpeg.exe）")
        enc = pick_video_encoder(self.encoders)
        if self._cancel.is_set():
            raise _Cancelled()
        dur = it.get("dur")
        cmd = build_ffmpeg_cmd(self.ffmpeg, src, dst, delta, enc,
                               it.get("audio_codec"), dur)
        ok, msg = run_ffmpeg(cmd, dur, prog_cb,
                             lambda line: self._ui_queue.put(("log", (line, "dim"))))
        if not ok:
            raise RuntimeError("ffmpeg 失败：" + (msg.splitlines()[-1] if msg else "未知错误"))
        return dst

    def _poll(self):
        """把工作线程的事件搬到 UI 线程。"""
        try:
            if not self.root.winfo_exists():
                return
        except Exception:
            # 窗口已销毁：Tcl 解释器里 _poll 命令已不存在，直接停掉轮询
            return
        if self._drop_sink:
            batch = self._drop_sink[:]
            del self._drop_sink[:]
            try:
                self._on_drop(batch)
            except Exception:
                self._log("处理拖入文件失败：%s" % sys.exc_info()[1], "err")
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "log":
                    self._log(*payload)
                elif kind == "row":
                    self._update_row(payload)
                elif kind == "prog":
                    val, txt = payload
                    self.prog.configure(value=val)
                    if txt:
                        self.prog_text.configure(text=txt)
                elif kind == "thumb":
                    key, path = payload
                    self._thumb_pending.discard(key)
                    if path and os.path.isfile(path):
                        try:
                            self._thumbs[key] = self.tk.PhotoImage(file=path)
                        except Exception:
                            self._thumbs[key] = False
                    else:
                        self._thumbs[key] = False
                    self._update_preview()
                elif kind == "done":
                    ok_cnt, total = payload
                    self._finish(ok_cnt, total)
        except queue.Empty:
            pass
        self._poll_id = self.root.after(80, self._poll)

    def _finish(self, ok_cnt, total):
        self.busy = False
        self.btn_run.configure(state="normal", bg=ACCENT)
        self.btn_stop.configure(state="disabled", bg=CARD2)
        self.prog_text.configure(text="")
        self.prog.configure(value=0)
        self._log("—— 完成：成功 %d / %d ——" % (ok_cnt, total), "ok" if ok_cnt == total else "warn")
        for it in self.items:
            if it["status"] == "完成":
                it["delta"] = 0
                it["status"] = "等待"
                it["tag"] = ""
                it["meta_rot"] = 0.0
        self._rebuild_tree()
        self.count_lbl.configure(text="%d 个文件" % len(self.items))
        d = (self.outdir.get() or "").strip()
        if ok_cnt and d and os.path.isdir(d):
            self.status.configure(text="输出目录：%s" % d)

    def _on_destroy(self, event):
        """窗口销毁时取消残留的轮询定时器（子控件销毁事件忽略）。"""
        if event.widget is not self.root:
            return
        try:
            self.root.after_cancel(self._poll_id)
        except Exception:
            pass

    def _on_close(self):
        if self.busy:
            if not self.messagebox.askyesno("确认退出", "正在处理中，确定要退出吗？"):
                return
            self._cancel.set()
        self._save_cfg()
        try:
            self.root.after_cancel(self._poll_id)
        except Exception:
            pass
        if self._thumb_dir:
            shutil.rmtree(self._thumb_dir, ignore_errors=True)
        self.root.destroy()


class _Cancelled(Exception):
    pass


# ======================================================================
#  五、自检（校验内置 MP4 引擎的偏移与矩阵数学）
# ======================================================================
def _build_dummy_mp4(path, w=1920, h=1080, version=0):
    def box(t, payload):
        return struct.pack(">I", 8 + len(payload)) + t + payload

    ident = rotation_to_matrix(0)
    mvhd_p = (b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, 1000, 5000)
              + struct.pack(">I", 0x00010000) + struct.pack(">H", 0x0100)
              + b"\x00" * 10 + ident + b"\x00" * 24 + struct.pack(">I", 2))
    if version == 1:
        # tkhd v1：creation/modification 8 字节，duration 8 字节
        tkhd_p = (b"\x01\x00\x00\x00" + struct.pack(">QQ", 0, 0)
                  + struct.pack(">I", 1) + b"\x00" * 4 + struct.pack(">Q", 5000)
                  + b"\x00" * 8 + struct.pack(">hhhh", 0, 0, 0, 0) + ident
                  + struct.pack(">II", int(w * 65536), int(h * 65536)))
    else:
        tkhd_p = (b"\x00\x00\x00\x00" + struct.pack(">II", 0, 0)
                  + struct.pack(">I", 1) + b"\x00" * 4 + struct.pack(">I", 5000)
                  + b"\x00" * 8 + struct.pack(">hhhh", 0, 0, 0, 0) + ident
                  + struct.pack(">II", int(w * 65536), int(h * 65536)))
    mdhd = b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, 1000, 5000) + struct.pack(">HH", 0x55C4, 0)
    hdlr = b"\x00\x00\x00\x00" + b"\x00" * 4 + b"vide" + b"\x00" * 12 + b"VideoHandler\x00"
    # 故意塞入真实文件里常见的干扰盒子，验证遍历器不会认错
    elst = b"\x00\x00\x00\x00" + struct.pack(">I", 1) + struct.pack(">IiI", 5000, 0, 0x00010000)
    stbl = box(b"stbl", box(b"stsd", b"\x00" * 8))
    minf = box(b"minf", box(b"vmhd", b"\x00" * 12) + stbl)
    mdia = box(b"mdia", box(b"mdhd", mdhd) + box(b"hdlr", hdlr) + minf)
    trak = box(b"trak", box(b"tkhd", tkhd_p) + box(b"edts", box(b"elst", elst)) + mdia)
    moov = box(b"moov", box(b"mvhd", mvhd_p) + trak + box(b"udta", box(b"free", b"")))
    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41")
    mdat = box(b"mdat", b"\x00" * 64)
    with open(path, "wb") as f:
        # moov 放最后（最常见的非 faststart 排布），前面加 free 盒子
        f.write(ftyp + box(b"free", b"\x00" * 8) + mdat + moov)


def selftest():
    import tempfile
    tmp = tempfile.mkdtemp(prefix="vrot_selftest_")
    print("=" * 62)
    print(" 内置 MP4 引擎自检")
    print("=" * 62)
    fails = 0
    for version in (0, 1):
        src = os.path.join(tmp, "src_v%d.mp4" % version)
        _build_dummy_mp4(src, 1920, 1080, version)
        info = locate_video_tkhd(src)
        theta, mir, sc = matrix_to_rotation(info["matrix"])
        print("tkhd v%d: 偏移=%d 矩阵偏移=%d 宽高=%dx%d 初始角度=%.1f"
              % (version, info["abs_offset"], info["matrix_off"],
                 info["width"], info["height"], theta))
        if (info["width"], info["height"]) != (1920, 1080):
            print("  ✘ 宽高解析错误"); fails += 1
        expect = [-90, -180, -90, 0]
        seq = [-90, -90, 90, 90]
        for delta, want in zip(seq, expect):
            dst = os.path.join(tmp, "out_v%d_%d.mp4" % (version, delta))
            metadata_rotate(src, dst, delta)
            got = read_mp4_meta(dst)["rotation"]
            good = abs(((got - want) % 360)) < 0.01 or abs(((got - want) % 360) - 360) < 0.01
            print("  +(%+d)° → 读回 %.1f°  (期望 %+.1f° 模 360) %s"
                  % (delta, got, want, "✔" if good else "✘"))
            if not good:
                fails += 1
            shutil.copy2(dst, src)   # 累积到下一轮

    # 符号锚定：这组常量是与 ffmpeg 实测对齐的（transpose 逐像素比对得出），
    # 往返测试对符号不敏感，必须单独钉死，防止符号再次被改反。
    anchors = {
        90:  (0, -65536, 65536, 0),          # 逆时针 90° = transpose=2
        -90: (0, 65536, -65536, 0),          # 顺时针 90° = transpose=1
        180: (-65536, 0, 0, -65536),
        0:   (65536, 0, 0, 65536),
    }
    for deg, want in anchors.items():
        m = struct.unpack(">5i", rotation_to_matrix(deg)[:20])
        got = (m[0], m[1], m[3], m[4])
        good = got == want
        print("符号锚定 %+4d°  (a,b,c,d)=%s %s" % (deg, got,
                                                  "✔" if good else "✘ 期望 %s" % (want,)))
        if not good:
            fails += 1

    # 字节级校验：只允许 36 字节矩阵区被改写，文件长度不变
    a = os.path.join(tmp, "byte_a.mp4")
    _build_dummy_mp4(a, 640, 360, 0)
    b = os.path.join(tmp, "byte_b.mp4")
    moff = locate_video_tkhd(a)["matrix_off"]
    metadata_rotate(a, b, -90)
    ba, bb = open(a, "rb").read(), open(b, "rb").read()
    diff = [i for i in range(min(len(ba), len(bb))) if ba[i] != bb[i]]
    in_range = all(moff <= i < moff + 36 for i in diff)
    print("文件长度 %d → %d；变更字节 %d 个，全部落在矩阵区[%d, %d)：%s"
          % (len(ba), len(bb), len(diff), moff, moff + 36,
             "✔" if (in_range and len(ba) == len(bb) and diff) else "✘"))
    if not (in_range and len(ba) == len(bb) and diff):
        fails += 1

    shutil.rmtree(tmp, ignore_errors=True)
    print("-" * 62)
    print("结果：%s" % ("全部通过 ✔" if fails == 0 else "%d 项失败 ✘" % fails))
    return 0 if fails == 0 else 1


# ======================================================================
def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    _enable_dpi()
    import tkinter as tk
    root = tk.Tk()
    try:
        VideoRotatorApp(root)
    except Exception:
        traceback.print_exc()
        try:
            from tkinter import messagebox
            messagebox.showerror("启动失败", traceback.format_exc())
        except Exception:
            pass
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
