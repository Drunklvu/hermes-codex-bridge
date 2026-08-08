"""Local A2A JSON-RPC bridge from Hermes to Codex CLI.

The bridge intentionally binds to loopback by default because Codex runs with
danger-full-access. It supports Hermes' v1-style PascalCase methods and the
legacy path-style aliases observed in older A2A clients.
"""

from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9998
DEFAULT_WORKSPACE = Path(os.environ.get("A2A_WORKSPACE", "C:\\Path\\To\\Your\\Workspace"))
DEFAULT_SYNC_WAIT = 540
DEFAULT_CODEX_TIMEOUT = 1800
MAX_BODY_BYTES = 1_048_576
MAX_RESULT_CHARS = 1_000_000
MAX_STDERR_CHARS = 120_000
STDOUT_TAIL_LINES = 200
EVENT_LOG_LINES = 500  # 每任务保留的结构化事件条数（实时监控用）
INBOUND_EVENT_LIMIT = 100  # inbound 任务事件条数上限
INBOUND_SUMMARY_CHARS = 400  # inbound message/reply 摘要长度上限
INBOUND_STUCK_TIMEOUT = 300  # inbound 任务 started 后 300s 无 finished 视为卡死（秒）
# 工作线健康阈值（warning 提示，rotate 由 P4 启用）
WS_WARN_TOKEN_BUDGET = 400_000   # 估算 token 预算（warning 60%）
WS_ROTATE_TOKEN_BUDGET = 500_000  # rotate 阈值（80%）
WS_WARN_MESSAGES = 50
WS_ROTATE_MESSAGES = 100
WS_WARN_IDLE_DAYS = 5
WS_ROTATE_IDLE_DAYS = 7
WS_WARN_FILE_BYTES = 2 * 1024 * 1024
WS_ROTATE_FILE_BYTES = 4 * 1024 * 1024
# 自动轮换开关：默认 False（warning-only），设 1 启用
WS_AUTO_ROTATE = os.environ.get("WS_AUTO_ROTATE", "0") == "1"
# 自动兜底：未传工作线时按 workspace+profile+时间窗短期复用（默认关）
WS_AUTO_EPHEMERAL = os.environ.get("WS_AUTO_EPHEMERAL", "0") == "1"
WS_EPHEMERAL_WINDOW_SECONDS = int(os.environ.get("WS_EPHEMERAL_WINDOW_SECONDS", "1800"))  # 30 分钟
HEARTBEAT_INTERVAL_SECONDS = 5.0
MAX_QUERY_RESULTS = 200
TERMINAL_STATES = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
}

MONITOR_UI_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Codex A2A 桥 · 实时监控</title>
<style>/* ═══ 夜航玻璃指挥台 · 视觉规范 ═══
   设计关键词：克制专业 + 玻璃拟态
   - 玻璃只做在面板容器上（sidebar / group / ov-card / ov-box / mbody / dialog），列表行（trow）不 blur，防大量渲染卡顿
   - 状态色降低饱和、只做语义标识：统计卡主数字统一冷白，状态色收敛为顶部 2px 指示线
   - 圆角体系：大面板 12 / 统计卡·输入框 10 / 徽章·按钮胶囊 999 / 任务行 8 */

:root {
  /* 底色：更深墨蓝（Codex 建议 #07101C），光斑低饱和放边缘 */
  --bg: #07101C;
  /* 玻璃面板：68% 不透明度（Codex 建议 .68），只核心面板使用 */
  --glass: rgba(18, 29, 44, .68);
  --glass-strong: rgba(14, 22, 34, .82);        /* 对话框等需要更高可读性的层级 */
  --glass-solid: #0D1520;                        /* 环图内芯等必须不透光的位置 */
  --input-bg: #152235;                           /* 输入框次级面板底 */
  --border: rgba(154, 178, 210, .14);            /* Codex 建议亮边框 */
  --border-strong: rgba(154, 178, 210, .22);
  /* 文字层级（Codex：正文 #DCE6F2 / 辅助 #94A3B8 / 标题 #F1F5F9，全部提亮） */
  --text: #DCE6F2;
  --text2: #94A3B8;
  --text3: #8291A6;
  /* 品牌色 */
  --blue: #78B8FF;
  --cyan: #58D6C2;
  /* 状态色（低饱和，仅做语义） */
  --ok: #58C890;       /* Completed */
  --warn: #E8B85C;     /* Working / Cancel */
  --err: #EF7777;      /* Failed / Rejected */
  --purple: #A98BFF;   /* Tool / 入站 */
  --sys: #8AA8A0;      /* System（灰绿） */
  /* 字体 */
  --mono: "Cascadia Code", Consolas, monospace;
  --sans: Inter, "Segoe UI", "Microsoft YaHei", sans-serif;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  background:
    radial-gradient(900px 600px at 10% -5%, rgba(120, 184, 255, .16), transparent 55%),
    radial-gradient(700px 500px at 95% 10%, rgba(88, 214, 194, .12), transparent 55%),
    radial-gradient(800px 600px at 50% 110%, rgba(169, 139, 255, .10), transparent 60%),
    radial-gradient(500px 400px at 30% 60%, rgba(120, 184, 255, .07), transparent 60%),
    var(--bg);
  background-attachment: fixed;
  color: var(--text);
  font-family: var(--sans);
  font-size: 14px;              /* 正文 14px（Codex：最低 12，深色背景要更大） */
  line-height: 1.6;
}

::selection { background: rgba(120, 184, 255, .25); }

:focus-visible { outline: 1px solid rgba(120, 184, 255, .5); outline-offset: 1px; }

/* ── 骨架 ── */
.layout { display: flex; height: 100vh; overflow: hidden; }

/* ── 左栏（玻璃面板容器） ── */
#sidebar {
  width: 300px; min-width: 300px;
  background: var(--glass);
  -webkit-backdrop-filter: blur(16px);
  backdrop-filter: blur(16px);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, .04), 0 8px 24px rgba(0, 0, 0, .18);
}

#sidehead { padding: 16px 16px 12px; border-bottom: 1px solid rgba(255, 255, 255, .05); }
#sidehead h1 {
  font-size: 20px; font-weight: 700;       /* 标题 20px/700 */
  letter-spacing: .2px;
  display: flex; align-items: center; gap: 10px;
}
#sidehead h1 .dot {
  width: 9px; height: 9px; border-radius: 50%;
  background: var(--ok);
  box-shadow: 0 0 10px rgba(88, 200, 144, .8);
}

/* ── 状态统计卡：主数字统一冷白，状态色只留顶部 2px 指示线 ── */
#stats {
  display: grid; grid-template-columns: repeat(5, 1fr);
  gap: 6px; padding: 12px 16px;
  border-bottom: 1px solid rgba(255, 255, 255, .05);
}
.stat {
  position: relative;
  background: rgba(10, 16, 26, .5);
  border: 1px solid var(--border);
  border-radius: 10px;                      /* 统计卡 10px */
  padding: 10px 6px 8px;
  text-align: center;
  overflow: hidden;
}
/* 顶部 2px 状态指示线 */
.stat::before {
  content: ""; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  border-radius: 2px 2px 0 0;
  background: var(--text3); opacity: .35;
}
.stat.c-accent::before { background: var(--blue); opacity: 1; }
.stat.c-ok::before     { background: var(--ok);    opacity: 1; }
.stat.c-err::before    { background: var(--err);   opacity: 1; }
.stat.c-warn::before   { background: var(--warn);  opacity: 1; }
.stat .n { font-size: 16px; font-weight: 700; line-height: 1.2; color: var(--text); } /* 统一冷白 */
.stat .l { font-size: 10px; color: var(--text3); text-transform: uppercase; letter-spacing: .5px; margin-top: 2px; }

/* ── 过滤区 ── */
#filters { display: flex; gap: 6px; padding: 10px 16px; border-bottom: 1px solid rgba(255,255,255,.05); flex-wrap: wrap; }
#filters input[type=search] {
  flex: 1; min-width: 120px;
  background: var(--input-bg);              /* 次级面板底 */
  border: 1px solid rgba(255, 255, 255, .08);
  color: var(--text);
  border-radius: 10px;                      /* 搜索框 10px，与按钮统一 */
  padding: 6px 10px;
  font-family: var(--sans); font-size: 12px;
  outline: none;
  transition: border-color .15s, box-shadow .15s;
}
#filters input[type=search]::placeholder { color: var(--text3); }
#filters input[type=search]:focus {
  border-color: rgba(120, 184, 255, .55);
  box-shadow: 0 0 0 3px rgba(120, 184, 255, .12);
}
.fbtn {
  background: rgba(10, 16, 26, .5);
  border: 1px solid var(--border);
  color: var(--text2);
  border-radius: 999px;                     /* 按钮胶囊 */
  padding: 5px 12px; font-size: 11px; font-weight: 500;
  cursor: pointer; font-family: var(--sans);
  transition: all .15s;
}
.fbtn:hover { border-color: var(--border-strong); color: var(--text); }
.fbtn.on {
  background: rgba(120, 184, 255, .14);
  border-color: rgba(120, 184, 255, .5);
  color: var(--blue);
  box-shadow: 0 0 12px rgba(120, 184, 255, .12);
}
/* ── 多选批量删除 ── */
#btn-multi.on { background: rgba(232, 184, 92, .14); border-color: rgba(232, 184, 92, .5); color: var(--warn); }
#multi-bar {
  display: flex; gap: 6px; padding: 8px 16px 10px;
  border-bottom: 1px solid rgba(255,255,255,.05);
  background: rgba(232, 184, 92, .06);
  align-items: center;
}
#multi-bar.hidden { display: none; }
#mb-del { border-color: rgba(239,119,119,.4); color: var(--err); }
#mb-del:hover { background: rgba(239,119,119,.15); }
.fbtn.danger { background: rgba(239,119,119,.1); }
.multi-cb {
  width: 15px; height: 15px; flex-shrink: 0; cursor: pointer;
  accent-color: var(--blue); margin: 0;
}
.group.multi-selected { border-left-color: var(--warn) !important; box-shadow: 0 0 0 1px rgba(232,184,92,.4); }
.group .ghead .multi-cb { display: none; }
.group.multi-mode .ghead .multi-cb { display: inline-block; margin-right: 2px; }

/* ── 任务列表 ── */
#tasklist { flex: 1; overflow-y: auto; padding: 8px; }
.refresh { color: var(--text3); font-size: 11px; padding: 4px 6px 8px; }
.empty {
  color: var(--text3); text-align: center;
  padding: 48px 12px; font-size: 12.5px;
}

/* 对话分组（玻璃面板容器，列表行本身不 blur） */
.group {
  position: relative;
  background: var(--glass);
  -webkit-backdrop-filter: blur(16px);
  backdrop-filter: blur(16px);
  border: 1px solid var(--border);
  border-left: 3px solid transparent;       /* 预留选中竖线宽度，避免布局跳动 */
  border-radius: 12px;                      /* 大面板 12px */
  margin-bottom: 8px;
  overflow: hidden;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, .03), 0 8px 24px rgba(0, 0, 0, .18);
  transition: border-color .15s, box-shadow .15s, background .15s;
}
.group:hover { border-color: rgba(255, 255, 255, .1); }
/* 选中态：仅 3px 品牌蓝左侧指示条（Codex：去掉重复描边光晕） */
.group.active {
  border-left-color: #60A5FA;
  background: #152235;
}

.ghead { display: flex; align-items: center; gap: 6px; padding: 9px 10px 9px 8px; cursor: pointer; }
.ghead:hover { background: rgba(255, 255, 255, .03); }
.ghead .caret { transition: transform .15s; font-size: 9px; color: var(--text3); flex-shrink: 0; }
.group.collapsed .caret { transform: rotate(-90deg); }
.ghead .del, .trow .del {
  background: none; border: none; color: var(--text3);
  cursor: pointer; font-size: 12px; padding: 2px 4px;
  border-radius: 6px; opacity: 0; transition: opacity .15s, color .15s, background .15s;
}
.ghead:hover .del, .trow:hover .del { opacity: 1; }
.ghead .del:hover, .trow .del:hover { color: var(--err); background: rgba(239, 119, 119, .12); }

.gtitle { flex: 1; min-width: 0; }
.gtitle .row1 { display: flex; align-items: center; gap: 6px; }
.gtitle .row2 { display: flex; align-items: center; gap: 8px; margin-top: 3px; }
.gname {
  font-weight: 600; font-size: 13px;          /* 组标题 13px/600 */
  color: var(--text);
  flex: 1; min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.gsub2 { color: var(--text3); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 170px; }

/* 状态徽章：胶囊 + 低饱和语义色 */
.badge {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 10.5px; font-weight: 600;
  padding: 2px 9px; border-radius: 999px;     /* 徽章胶囊 */
  white-space: nowrap; flex-shrink: 0;
  border: 1px solid transparent;
}
.badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.badge.COMPLETED { color: var(--ok);    background: rgba(88, 200, 144, .12); border-color: rgba(88, 200, 144, .18); }
.badge.FAILED    { color: var(--err);   background: rgba(239, 119, 119, .12); border-color: rgba(239, 119, 119, .18); }
.badge.REJECTED  { color: var(--err);   background: rgba(239, 119, 119, .12); border-color: rgba(239, 119, 119, .18); }
.badge.CANCELED  { color: var(--warn);  background: rgba(232, 184, 92, .1);  border-color: rgba(232, 184, 92, .16); }
.badge.WORKING   { color: var(--warn);  background: rgba(232, 184, 92, .12); border-color: rgba(232, 184, 92, .2); }
.badge.UNKNOWN   { color: var(--text2); background: rgba(168, 181, 197, .08); }

.cnt {
  font-size: 10.5px; color: var(--text3);
  background: rgba(255, 255, 255, .04);
  border: 1px solid rgba(255, 255, 255, .06);
  padding: 1px 8px; border-radius: 999px;
  white-space: nowrap; flex-shrink: 0;   /* 防 flex 压缩换行（竖排修复）*/
}

/* 方向芯片（胶囊） */
.dirchip { font-size: 10px; font-weight: 500; padding: 2px 8px; border-radius: 999px; white-space: nowrap; }
.dirchip.outb  { background: rgba(120, 184, 255, .12); color: var(--blue);   border: 1px solid rgba(120, 184, 255, .16); }
.dirchip.inb   { background: rgba(169, 139, 255, .12); color: var(--purple); border: 1px solid rgba(169, 139, 255, .16); }
.dirchip.noise { background: rgba(168, 181, 197, .08); color: var(--text3); }

.gen { font-size: 10px; color: var(--warn); background: rgba(232, 184, 92, .1); border: 1px solid rgba(232, 184, 92, .16); padding: 1px 6px; border-radius: 999px; }

/* 摘要：12px/1.45，两行截断 */
.gsummary {
  padding: 0 10px 8px; color: var(--text2);
  font-size: 12px; line-height: 1.45;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
  overflow: hidden;
  cursor: help;
}

.gsub { border-top: 1px solid rgba(255, 255, 255, .05); }
.gsub.collapsed { display: none; }
.group.collapsed .gsub { display: none; }

/* 任务行：不 blur，轻量 hover */
.trow {
  display: flex; align-items: center; gap: 6px;
  padding: 4px 10px; font-size: 11.5px; color: var(--text2);
  border-radius: 8px;                       /* 任务行 8px */
  transition: background .12s;
}
.trow:hover { background: rgba(255, 255, 255, .035); }
.trow .id { font-family: var(--mono); color: var(--text3); font-size: 11px; }   /* ID：等宽 11px */
.trow .gw { font-size: 10px; color: #A08B6A; }
.trow .ts, .gtitle .row2 .ts { margin-left: auto; font-size: 11px; font-weight: 500; color: var(--text3); white-space: nowrap; } /* 时间戳 11px/500 低对比 */

.group.noise-group { opacity: .85; border-style: solid; }
.group.noise-group .ghead { background: rgba(168, 181, 197, .04); }

/* ── 右栏 ── */
#main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
#mainhead {
  padding: 6px 20px;
  border-bottom: 1px solid rgba(255, 255, 255, .06);
  display: flex; align-items: center; gap: 10px; min-height: 34px;
  background: rgba(18, 29, 44, .5);
}
#conn {
  font-size: 13px; font-weight: 600; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; cursor: pointer;
  border-radius: 8px; padding: 2px 8px; margin-left: -8px;
  transition: background .15s;
}
#conn:hover { background: rgba(120, 184, 255, .1); }
#mainhead .hdr-meta { font-size: 11px; color: var(--text3); font-family: var(--mono); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 320px; cursor: help; }
#events { flex: 1; overflow-y: auto; padding: 16px 20px; background: radial-gradient(800px 400px at 90% 0%, rgba(34, 211, 238, .04), transparent 60%), #091522; }

/* ── 总览 ── */
#overview { max-width: 760px; margin: 0 auto; padding: 20px 0; }
#overview h2 { font-size: 20px; font-weight: 700; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }
#overview h2 .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 10px rgba(88, 200, 144, .8); }

/* 统计卡（玻璃） */
.ov-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 18px; }
.ov-card {
  position: relative;
  background: var(--glass);
  -webkit-backdrop-filter: blur(16px);
  backdrop-filter: blur(16px);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 16px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, .03), 0 8px 24px rgba(0, 0, 0, .18);
  overflow: hidden;
}
.ov-card .n { font-size: 22px; font-weight: 700; line-height: 1.2; color: var(--text); position: relative; } /* 主数字统一冷白 */
.ov-card .l { font-size: 11px; color: var(--text3); margin-top: 2px; }
/* 状态色只保留顶部 2px 指示线 */
.ov-card .n::before {
  content: ""; position: absolute; top: -14px; left: -16px; right: -16px; height: 2px;
  background: var(--text3); opacity: .3;
}
.ov-card .n.c-accent::before { background: var(--blue); opacity: 1; }
.ov-card .n.c-ok::before     { background: var(--ok);   opacity: 1; }
.ov-card .n.c-err::before    { background: var(--err);  opacity: 1; }

.ov-flex { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 18px; }
.ov-box {
  background: var(--glass);
  -webkit-backdrop-filter: blur(16px);
  backdrop-filter: blur(16px);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 16px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, .03), 0 8px 24px rgba(0, 0, 0, .18);
}
.ov-box h3 {
  font-size: 14px; font-weight: 600;         /* 区域标题 14px/600 */
  color: var(--text2);
  margin-bottom: 10px;
  letter-spacing: .5px;
  display: flex; align-items: center; gap: 6px;
}
.ov-box h3::before { content: ""; width: 3px; height: 12px; border-radius: 2px; background: rgba(120, 184, 255, .6); }

/* 堆叠条（低饱和语义色） */
.stackbar { display: flex; height: 6px; border-radius: 3px; overflow: hidden; margin-bottom: 10px; background: rgba(255, 255, 255, .04); }
.stackbar div { height: 100%; transition: width .3s; }
.stackbar .s-ok    { background: var(--ok); }
.stackbar .s-err   { background: var(--err); }
.stackbar .s-warn  { background: var(--warn); }
.stackbar .s-accent{ background: var(--blue); }

.legend { display: flex; flex-wrap: wrap; gap: 10px; font-size: 11px; color: var(--text2); }
.legend .lg { display: flex; align-items: center; gap: 4px; }
.legend .lg::before { content: ""; width: 8px; height: 8px; border-radius: 2px; }
.legend .lg.lg-ok::before    { background: var(--ok); }
.legend .lg.lg-err::before   { background: var(--err); }
.legend .lg.lg-warn::before  { background: var(--warn); }
.legend .lg.lg-accent::before{ background: var(--blue); }

/* 环图（内芯用不透光色，避免玻璃透出环色） */
.donut-wrap { display: flex; align-items: center; gap: 14px; }
.donut { width: 88px; height: 88px; border-radius: 50%; position: relative; flex-shrink: 0; }
.donut::after { inset: 14px; }
.donut::after { content: ""; position: absolute; inset: 14px; background: var(--glass-solid); border-radius: 50%; }
.donut .dlabel {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 700; z-index: 1; color: var(--text);
}
.donut-legend { font-size: 11.5px; color: var(--text2); display: flex; flex-direction: column; gap: 4px; }
.donut-legend .dl { display: flex; align-items: center; gap: 6px; }
.donut-legend .dl::before { content: ""; width: 8px; height: 8px; border-radius: 50%; }
.donut-legend .dl.dl-in::before  { background: var(--purple); }
.donut-legend .dl.dl-out::before { background: var(--blue); }

/* 最近活动 */
.recent { display: flex; flex-direction: column; gap: 4px; min-height: 240px; max-height: 340px; overflow-y: auto; }
.recent .rrow {
  display: flex; align-items: center; gap: 8px;
  font-size: 11.5px; color: var(--text2);
  padding: 4px 0; border-bottom: 1px solid rgba(255, 255, 255, .05);
}
.recent .rrow:last-child { border-bottom: none; }
.recent .rname { font-family: var(--mono); font-size: 11px; color: var(--text3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 160px; }

/* ── 对话时间轴 ── */
.back-btn {
  background: var(--glass);
  -webkit-backdrop-filter: blur(8px);
  backdrop-filter: blur(8px);
  border: 1px solid var(--border);
  color: var(--text2);
  border-radius: 999px;                     /* 按钮胶囊 */
  padding: 5px 14px; font-size: 12px; font-weight: 500;
  cursor: pointer; margin-bottom: 14px; font-family: var(--sans);
  transition: all .15s;
}
.back-btn:hover { border-color: rgba(120, 184, 255, .5); color: var(--blue); box-shadow: 0 0 12px rgba(120, 184, 255, .1); }

/* 日期分隔条 */
.date-sep {
  text-align: center; font-size: 11px; color: var(--text3);
  margin: 4px 0 10px; padding: 3px 12px;
  background: rgba(255, 255, 255, .03); border-radius: 999px;
  align-self: center;
}
/* ── 调用切换器（同会话多次记录回顾） ── */
.call-switcher {
  display: flex; align-items: center; gap: 6px;
  max-width: 860px; margin: 0 auto 14px; padding: 6px 10px;
  background: rgba(255, 255, 255, .03);
  border: 1px solid var(--border); border-radius: 10px;
  flex-wrap: wrap;
}
.cs-label { font-size: 11px; color: var(--text3); }
.cs-btn {
  background: rgba(255, 255, 255, .04); border: 1px solid var(--border);
  color: var(--text2); border-radius: 999px; padding: 3px 11px;
  font-size: 11px; cursor: pointer; font-family: var(--sans);
  transition: all .15s;
}
.cs-btn:hover { border-color: var(--border-strong); color: var(--text); }
.cs-btn.on { background: rgba(120, 184, 255, .14); border-color: rgba(120, 184, 255, .5); color: var(--blue); }

/* ── 对话区：社交软件式气泡 ── */
.chat { max-width: 860px; margin: 0 auto; display: flex; flex-direction: column; gap: 18px; padding: 0 24px 24px; }
.chat-row { display: flex; align-items: flex-end; gap: 8px; }
.chat-row.them { justify-content: flex-start; }
.chat-row.me { justify-content: flex-end; }
.chat-row.sys { justify-content: center; }
/* 头像：圆形底 + SVG 图标 */
.avatar {
  width: 30px; height: 30px; border-radius: 50%; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  background: var(--glass-strong); border: 1px solid var(--border);
  color: var(--text3); margin-bottom: 2px;
}
.chat-row.them .avatar { color: #5EEAD4; background: #123B43; border-color: rgba(94, 234, 212, .25); }
.chat-row.me .avatar { color: #93C5FD; background: #132F4B; border-color: rgba(147, 197, 253, .25); }
/* 气泡主体 */
.bubble {
  max-width: 68%; min-width: 60px; padding: 12px 16px;
  border-radius: 12px; position: relative;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, .03), 0 4px 16px rgba(0, 0, 0, .15);
}
/* 气泡无尾尖：方向由左右布局 + 非对称圆角表达 */
/* 角色配色 */
.chat-row.them .bubble { background: rgba(20, 32, 48, .82); border: 1px solid rgba(126, 153, 185, .20); color: var(--text); border-bottom-left-radius: 5px; }
.chat-row.me .bubble { background: #256AA5; color: #F7FAFC; border: 1px solid rgba(147, 197, 253, .24); border-bottom-right-radius: 5px; }
.chat-row.me .bubble .bmeta { color: rgba(255, 255, 255, .72); }
.chat-row.them .bubble .bmeta { color: var(--text3); }
/* 工具/系统事件卡（独立于聊天流） */
.chat-event {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  max-width: 860px; margin: 0 auto 10px;
  background: #111C2A; border: 1px solid #2A3A4F;
  border-radius: 10px; padding: 7px 12px;
  color: var(--text3); font-size: 11.5px;
}
.chat-event svg { color: var(--purple); flex-shrink: 0; }
.evt-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--purple); flex-shrink: 0; }
.evt-name { font-weight: 600; color: var(--text2); }
.evt-body {
  width: 100%; margin-top: 4px;
  font-family: var(--mono); font-size: 12px; line-height: 18px;
  color: var(--text3); max-height: 120px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-word;
}
/* 气泡文本 + 时间 */
.btext { font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; max-height: 320px; overflow-y: auto; }
.btext.long { max-height: 220px; overflow: hidden; position: relative; }
.expand-btn {
  background: none; border: none; color: var(--blue);
  font-size: 11px; cursor: pointer; padding: 2px 0 0;
  font-family: var(--sans); text-decoration: underline;
}
.bmeta { font-size: 11px; line-height: 16px; margin-top: 8px; text-align: right; white-space: nowrap; }

/* ── 确认对话框（玻璃强层级） ── */
dialog {
  background: var(--glass-strong);
  -webkit-backdrop-filter: blur(20px);
  backdrop-filter: blur(20px);
  color: var(--text);
  border: 1px solid rgba(255, 255, 255, .1);
  border-radius: 12px;
  padding: 0; width: 380px;
  box-shadow: 0 24px 64px rgba(0, 0, 0, .5), inset 0 1px 0 rgba(255, 255, 255, .05);
}
dialog::backdrop { background: rgba(4, 8, 14, .6); -webkit-backdrop-filter: blur(4px); backdrop-filter: blur(4px); }
.dlg-body { padding: 18px 20px; }
.dlg-body h3 { font-size: 14px; font-weight: 600; margin-bottom: 8px; }
.dlg-body p { font-size: 12.5px; color: var(--text2); line-height: 1.6; }
.dlg-body .dlg-warn { color: var(--err); font-size: 12px; margin-top: 8px; }
.dlg-actions {
  display: flex; justify-content: flex-end; gap: 8px;
  padding: 12px 20px; border-top: 1px solid rgba(255, 255, 255, .06);
}
.dlg-actions button {
  border: 1px solid var(--border);
  background: rgba(24, 34, 49, .8);
  color: var(--text);
  border-radius: 999px;                     /* 按钮胶囊 */
  padding: 6px 16px; font-size: 12.5px; font-weight: 500;
  cursor: pointer; font-family: var(--sans);
  transition: all .15s;
}
.dlg-actions button:hover { border-color: var(--border-strong); }
.dlg-actions .danger {
  background: rgba(239, 119, 119, .12);
  border-color: rgba(239, 119, 119, .4);
  color: var(--err);
}
.dlg-actions .danger:hover { background: rgba(239, 119, 119, .22); border-color: rgba(239, 119, 119, .6); }
.warn-badge {
  display: inline-block;
  background: rgba(239, 119, 119, .12); color: var(--err);
  border: 1px solid rgba(239, 119, 119, .35);
  font-size: 10.5px; padding: 1px 9px; border-radius: 999px;
}

/* sparkline */
.spark { display: block; margin-top: 6px; }

/* ── 滚动条（低对比细条） ── */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, .1); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: rgba(255, 255, 255, .2); }
::-webkit-scrollbar-track { background: transparent; }

/* ── 窄屏降级：取消玻璃，保证可用性 ── */
@media (max-width: 900px) {
  .layout { flex-direction: column; }
  #sidebar { width: 100%; min-width: 0; max-height: 45vh; }
  #stats { grid-template-columns: repeat(5, 1fr); }
  #main { min-height: 55vh; }
  .group, .ov-card, .ov-box, .tlmsg .mbody { -webkit-backdrop-filter: none; backdrop-filter: none; }
}

/* ── 顶栏 Header（玻璃） ── */
.layout { flex-direction: column; }
.layout-body { display: flex; flex: 1; min-height: 0; }
#topbar {
  display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 12px;
  padding: 0 20px; min-height: 60px; flex-shrink: 0;
  background: var(--glass-strong);
  -webkit-backdrop-filter: blur(16px); backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--border);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.04), 0 8px 24px rgba(0,0,0,.18);
  position: relative; z-index: 5;
}
.brand { justify-self: start; display: flex; align-items: center; gap: 10px; min-width: 0; }
.brand-dot { width: 9px; height: 9px; border-radius: 50%;
  background: linear-gradient(135deg, var(--blue), var(--cyan));
  box-shadow: 0 0 10px rgba(120,184,255,.55); }
.brand-txt { display: flex; flex-direction: column; line-height: 1.25; min-width: 0; }
.brand-name { font-size: 15px; font-weight: 700; color: var(--text); letter-spacing: .2px; white-space: nowrap; }
.brand-sub { font-size: 11px; color: var(--text3); white-space: nowrap; margin-top: 1px; }
.topbar-search { justify-self: center; width: min(360px, 32vw); }
.topbar-search input[type=search] {
  width: 100%; height: 36px; background: var(--input-bg); border: 1px solid var(--border);
  color: var(--text); border-radius: 10px; padding: 6px 14px;
  font-family: var(--sans); font-size: 12.5px; outline: none;
  transition: border-color .15s, box-shadow .15s;
}
.topbar-search input[type=search]::placeholder { color: var(--text3); }
.topbar-search input[type=search]:focus { border-color: rgba(120,184,255,.55); box-shadow: 0 0 0 3px rgba(120,184,255,.12); }
.topbar-right { justify-self: end; display: flex; align-items: center; gap: 10px; }
.conn-state { width: 8px; height: 8px; border-radius: 50%; background: var(--warn); box-shadow: 0 0 6px rgba(232,184,92,.6); }
.conn-state.ok { background: var(--ok); box-shadow: 0 0 6px rgba(88,200,144,.6); }
.conn-state.err { background: var(--err); box-shadow: 0 0 6px rgba(239,119,119,.6); }
.conn-text { font-size: 11px; color: var(--text2); font-weight: 500; white-space: nowrap; }
.last-refresh { font-size: 10.5px; color: var(--text3); white-space: nowrap; }
#btn-refresh { width: 32px; height: 32px; padding: 0; font-size: 14px; display: inline-flex; align-items: center; justify-content: center; }
/* 4 卡统计：grid 4 列 */
#stats { grid-template-columns: repeat(4, 1fr); }
/* 代次数字圆环 */
.gen { border-radius: 50%; width: 22px; height: 22px; display: inline-flex; align-items: center; justify-content: center; font-family: var(--mono); font-size: 10px; }
/* 方向芯片内 SVG 对齐 */
.dirchip svg, .caret svg { display: block; }
</style>
</head>
<body>
<div class="layout">
  <header id="topbar">
    <div class="brand">
      <span class="brand-dot"></span>
      <div class="brand-txt">
        <span class="brand-name">Codex A2A Monitor</span>
        <span class="brand-sub">本地双向 Agent 协作桥</span>
      </div>
    </div>
    <div class="topbar-search">
      <input type="search" id="q" placeholder="搜索 context / 摘要 / ID…" title="搜索">
    </div>
    <div class="topbar-right">
      <span class="conn-state" id="conn-dot"></span>
      <span class="conn-text" id="conn-text">Polling</span>
      <span class="last-refresh" id="last-refresh"></span>
      <button class="fbtn" id="btn-refresh" title="手动刷新">⟳</button>
    </div>
  </header>
  <div class="layout-body">
    <div id="sidebar">
      <div id="filters">
        <button class="fbtn" id="f-all" onclick="setFilter('state','')">全部</button>
        <button class="fbtn" id="f-wk" onclick="setFilter('state','WORKING')">进行中</button>
        <button class="fbtn" id="f-err" onclick="setFilter('state','FAILED')">失败</button>
        <button class="fbtn" id="f-ok" onclick="setFilter('state','COMPLETED')">完成</button>
        <button class="fbtn" id="btn-multi" onclick="toggleMultiSelect()" title="勾选多个对话批量删除">☑ 多选</button>
      </div>
      <div id="multi-bar" class="hidden">
        <button class="fbtn" id="mb-all" onclick="toggleSelectAll()">全选</button>
        <button class="fbtn danger" id="mb-del" onclick="confirmMultiDelete()">删除选中 (<span id="mb-count">0</span>)</button>
        <button class="fbtn" id="mb-cancel" onclick="toggleMultiSelect()">取消</button>
      </div>
      <div id="tasklist"><div class="refresh">对话列表（点击查看，同对话自动分组）</div></div>
    </div>
    <div id="main">
      <div id="mainhead">
        <span id="conn" title="点击回到总览">首页 / 总览</span>
        <span class="hdr-meta" id="connmeta"></span>
      </div>
      <div id="events"></div>
    </div>
  </div>
</div>
<dialog id="confirmDlg">
  <div class="dlg-body">
    <h3 id="dlg-title">确认删除</h3>
    <p id="dlg-desc"></p>
    <div class="dlg-warn" id="dlg-warn"></div>
  </div>
  <div class="dlg-actions">
    <button id="dlg-cancel">取消</button>
    <button class="danger" id="dlg-ok">删除</button>
  </div>
</dialog>
<script>
const $ = (id) => document.getElementById(id);
function fmtTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const p = (n) => String(n).padStart(2, "0");
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate())
      + " " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  } catch (e) { return iso; }
}
// 只显示时分（气泡内时间）
function fmtClock(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const p = (n) => String(n).padStart(2, "0");
    return p(d.getHours()) + ":" + p(d.getMinutes());
  } catch (e) { return iso; }
}
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}
// ============ 内嵌 SVG 图标工厂（替代 emoji；currentColor 继承配色） ============
const SVG_NS = "http://www.w3.org/2000/svg";
function svgIcon(paths, size) {
  const s = document.createElementNS(SVG_NS, "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("width", size);
  s.setAttribute("height", size);
  s.setAttribute("fill", "none");
  s.setAttribute("stroke", "currentColor");
  s.setAttribute("stroke-width", "2");
  s.setAttribute("stroke-linecap", "round");
  s.setAttribute("stroke-linejoin", "round");
  s.setAttribute("aria-hidden", "true");
  s.innerHTML = paths;
  return s;
}
const ICONS = {
  chevron: '<path d="M6 9l6 6 6-6"/>',
  arrowR:  '<path d="M4 12h16m0 0l-6-6m6 6l-6 6"/>',
  arrowL:  '<path d="M20 12H4m0 0l6-6m-6 6l6 6"/>',
  beaker:  '<path d="M4.5 3h15M6 3v16a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V3M6 14h12"/>',
  trash:   '<path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>',
  user:    '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  bot:     '<rect x="5" y="9" width="14" height="10" rx="2"/><path d="M12 9V5m0 0l-2.5 2.5M12 5l2.5 2.5"/><circle cx="9.5" cy="13.5" r=".8"/><circle cx="14.5" cy="13.5" r=".8"/>',
  wrench:  '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
  pulse:   '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>'
};
const ROLE_META = {
  user:      { cls: "u", label: "用户", icon: ICONS.user },
  assistant: { cls: "a", label: "助手", icon: ICONS.bot },
  tool:      { cls: "t", label: "工具", icon: ICONS.wrench },
  system:    { cls: "s", label: "系统", icon: ICONS.pulse }
};

const BADGE_STATES = new Set(["COMPLETED", "FAILED", "REJECTED", "CANCELED", "WORKING", "UNKNOWN"]);
let current = null, lastSeq = -1, polling = false, currentCtx = null, convTimer = null;
const TOKEN = null;
let filterState = "", filterDir = "", searchQ = "";
let allTasks = [];

function setFilter(kind, val) {
  if (kind === "state") { filterState = val; ["f-all","f-wk","f-err","f-ok"].forEach(i=>$(i).classList.toggle("on", $(i).id === "f-all" ? val==="" : (i==="f-wk"?val==="WORKING":i==="f-err"?val==="FAILED":val==="COMPLETED"))); }
  refreshTasks();
}
$("q").addEventListener("input", () => { searchQ = $("q").value.trim().toLowerCase(); refreshTasks(); });
$("conn").addEventListener("click", showOverview);

function renderOverview(tasks) {
  const ev = $("events");
  const box = el("div", "", null);
  const h = el("h2", "", null);
  h.appendChild(el("span", "dot", null));
  h.appendChild(document.createTextNode("总览"));
  box.appendChild(h);
  // 统计卡片
  const cards = el("div", "ov-cards", null);
  const total = tasks.length;
  const done = tasks.filter(t=>t.state==="COMPLETED").length;
  const failed = tasks.filter(t=>t.state==="FAILED"||t.state==="REJECTED").length;
  const working = tasks.filter(t=>t.state==="WORKING").length;
  const mk = (n,l,c) => { const d=el("div","ov-card",null); d.appendChild(el("div","n "+c,n)); d.appendChild(el("div","l",l)); return d; };
  cards.appendChild(mk(total,"全部对话","c-accent"));
  cards.appendChild(mk(done,"已完成","c-ok"));
  cards.appendChild(mk(failed,"失败","c-err"));
  cards.appendChild(mk(working,"进行中","c-accent"));
  box.appendChild(cards);
  // 状态分布 + 方向环图
  const flex = el("div", "ov-flex", null);
  const stBox = el("div", "ov-box", null);
  stBox.appendChild(el("h3", "", "状态分布"));
  const bar = el("div", "stackbar", null);
  const mkSeg = (n, cls) => { if (!n) return; const s = el("div", cls, null); s.style.width = (n/total*100).toFixed(1) + "%"; bar.appendChild(s); };
  mkSeg(done, "s-ok"); mkSeg(failed, "s-err"); mkSeg(tasks.filter(t=>t.state==="CANCELED").length, "s-warn"); mkSeg(working, "s-accent");
  stBox.appendChild(bar);
  const lg = el("div", "legend", null);
  [["完成",done,"lg-ok"],["失败",failed,"lg-err"],["取消",tasks.filter(t=>t.state==="CANCELED").length,"lg-warn"],["进行中",working,"lg-accent"]].forEach(([l,n,c])=>{
    const x = el("span","lg "+c,null); x.appendChild(document.createTextNode(l+" "+n)); lg.appendChild(x);
  });
  stBox.appendChild(lg);
  flex.appendChild(stBox);
  // 方向环图
  const dBox = el("div", "ov-box", null);
  dBox.appendChild(el("h3", "", "方向比例"));
  const inb = tasks.filter(t=>t.direction==="inbound").length;
  const outb = tasks.length - inb;
  const dw = el("div", "donut-wrap", null);
  const donut = el("div", "donut", null);
  if (total > 0) {
    const inPct = (inb/total*100).toFixed(1);
    donut.style.background = "conic-gradient(var(--blue) 0% " + inPct + "%, var(--cyan) " + inPct + "% 100%)";
    donut.appendChild(el("div","dlabel", inPct + "%"));
  } else {
    donut.style.background = "conic-gradient(var(--border) 0% 100%)";
    donut.appendChild(el("div","dlabel","0%"));
  }
  dw.appendChild(donut);
  const dl = el("div", "donut-legend", null);
  const m1 = el("div","dl dl-in",null); m1.appendChild(document.createTextNode("Codex→Hermes " + inb)); dl.appendChild(m1);
  const m2 = el("div","dl dl-out",null); m2.appendChild(document.createTextNode("Hermes→Codex " + outb)); dl.appendChild(m2);
  dw.appendChild(dl);
  dBox.appendChild(dw);
  flex.appendChild(dBox);
  box.appendChild(flex);
  // 最近活动
  const rBox = el("div", "ov-box", null);
  rBox.appendChild(el("h3", "", "最近活动"));
  const rc = el("div", "recent", null);
  [...tasks].sort((a,b)=>(b.created_at||"").localeCompare(a.created_at||"")).slice(0,6).forEach(t=>{
    const r = el("div","rrow",null);
    r.appendChild(el("span","badge "+(BADGE_STATES.has(t.state)?t.state:"UNKNOWN"), t.state));
    const rn = el("span","rname", (t.contextId||"?"));
    rn.title = t.contextId || "";
    r.appendChild(rn);
    r.appendChild(el("span","ts", fmtTime(t.created_at)));
    rc.appendChild(r);
  });
  rBox.appendChild(rc);
  box.appendChild(rBox);
  ev.replaceChildren(box);
}

function renderEmpty(msg) {
  const ev = $("events");
  ev.replaceChildren(el("div", "empty", msg));
}

function showOverview() {
  current = null; currentCtx = null; lastSeq = -1; polling = false;
  if (convTimer) { clearInterval(convTimer); convTimer = null; }
  $("conn").textContent = "首页 / 总览";
  $("connmeta").textContent = "";
  refreshTasks();
}

function renderConversation(messages, source) {
  const ev = $("events");
  if (!messages || !messages.length) {
    const hint = source === "inbound-report" ? "反向调用（Codex→Hermes），暂无内容摘要" : "该任务无对话记录（历史会话可能已清理）";
    ev.replaceChildren(el("div", "empty", hint));
    return;
  }
  const back = el("button", "back-btn", "← 返回总览");
  back.onclick = showOverview;
  ev.replaceChildren(back);
  // 调用切换器：当前 context 的所有调用记录，可切换回顾
  const switcher = el("div", "call-switcher", null);
  const ctxTasks = (allTasks || []).filter(t => (t.contextId || "") === currentCtx);
  if (ctxTasks.length > 1) {
    const label = el("span", "cs-label", "调用 " + ctxTasks.length + " 次：");
    switcher.appendChild(label);
    ctxTasks.slice().sort((a, b) => (a.created_at || "").localeCompare(b.created_at || "")).forEach((t, i) => {
      const btn = el("button", "cs-btn" + (t.id === current ? " on" : ""), "#" + (i + 1));
      btn.title = fmtTime(t.created_at) + " · " + (t.state || "");
      btn.onclick = () => { current = t.id; loadConversation(t.id); };
      switcher.appendChild(btn);
    });
    ev.appendChild(switcher);
  }
  const tl = el("div", "chat", null);
  let lastDate = "";
  for (const m of messages) {
    const dStr = (m.ts || "").slice(0, 10);
    if (dStr && dStr !== lastDate) {
      lastDate = dStr;
      tl.appendChild(el("div", "date-sep", dStr));
    }
    const role = m.role || "system";
    const meta = ROLE_META[role] || ROLE_META.system;
    // 工具/系统消息：独立事件卡（图标+工具名+折叠输出）
    if (role === "tool" || role === "system") {
      const evt = el("div", "chat-event", null);
      const eico = svgIcon(meta.icon, 13);
      evt.appendChild(eico);
      const eico2 = el("span", "evt-dot", null);
      evt.appendChild(eico2);
      const etxt = el("span", "evt-name", (role === "tool" ? "工具" : "系统") + (m.name ? " · " + m.name : ""));
      evt.appendChild(etxt);
      const ebody = el("div", "evt-body", m.text || "");
      evt.appendChild(ebody);
      tl.appendChild(evt);
      continue;
    }
    // 社交软件式气泡：user 右侧，assistant 左侧
    const side = role === "user" ? "me" : "them";
    const msg = el("div", "chat-row " + side, null);
    // 头像（SVG 图标圆形底）
    const avatar = el("span", "avatar", null);
    const ico = svgIcon(meta.icon, 14);
    ico.setAttribute("aria-label", meta.label);
    avatar.appendChild(ico);
    // 气泡主体
    const bubble = el("div", "bubble", null);
    const btext = el("div", "btext", m.text || "");
    if ((m.text || "").length > 600) {
      btext.classList.add("long");
      const expand = el("button", "expand-btn", "展开全文");
      expand.onclick = () => { btext.classList.toggle("long"); expand.textContent = btext.classList.contains("long") ? "展开全文" : "收起"; };
      bubble.appendChild(btext);
      bubble.appendChild(expand);
    } else {
      bubble.appendChild(btext);
    }
    const bmeta = el("div", "bmeta", (role === "user" ? "Hermes" : "Codex") + " · " + fmtClock(m.ts));
    bubble.appendChild(bmeta);
    // 组装：user 头像在右（先气泡后头像），assistant 头像在左
    if (side === "me") { msg.appendChild(bubble); msg.appendChild(avatar); }
    else { msg.appendChild(avatar); msg.appendChild(bubble); }
    tl.appendChild(msg);
  }
  ev.appendChild(tl);
}

async function refreshTasks() {
  try {
    const r = await fetch("/tasks");
    const d = await r.json();
    allTasks = d.tasks || [];
    let tasks = allTasks;
    if (filterState) tasks = tasks.filter(t => t.state === filterState);
    if (searchQ) tasks = tasks.filter(t => {
      const searchable = (t.contextId||"") + " " + (t.summary||t.message_summary||"") + " " + (t.id||"")
        + (t.noise ? " 测试噪音 noise" : "");
      return searchable.toLowerCase().includes(searchQ);
    });
    const box = $("tasklist");
    box.replaceChildren(el("div", "refresh", "对话列表（点击查看，同对话自动分组）"));
  if (multiMode) updateMultiBar();
    if (!tasks.length) {
      box.appendChild(el("div", "empty", filterState || searchQ ? "无匹配对话" : "暂无任务"));
      if (!current) renderOverview(allTasks);
      return;
    }
    currentGroups = [];
    const groups = new Map();
    const noise = [];
    for (const t of tasks) {
      if (t.noise) { noise.push(t); continue; }
      const key = t.contextId || "(no-context)";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(t);
    }
    if (noise.length) groups.set("__NOISE__", noise);
    for (const [ctx, gtasks] of groups) {
      const isNoise = ctx === "__NOISE__";
      const gdiv = el("div", "group" + (ctx === currentCtx && !isNoise ? " active" : "")
        + (isNoise ? " noise-group collapsed" : ""));
      const ghead = el("div", "ghead", null);

      currentGroups.push({ ctx, isNoise });
      if (multiMode) gdiv.classList.add("multi-mode");
      const cb = el("input", "multi-cb", null);
      cb.type = "checkbox";
      cb.checked = selectedCtxs.has(ctx);
      cb.onclick = (ev) => { ev.stopPropagation(); if (selectedCtxs.has(ctx)) selectedCtxs.delete(ctx); else selectedCtxs.add(ctx); cb.checked = selectedCtxs.has(ctx); gdiv.classList.toggle("multi-selected", selectedCtxs.has(ctx)); updateMultiBar(); };
      gdiv.onclick = (ev) => {
        if (multiMode) { ev.stopPropagation(); cb.click(); return; }
        selectContext(ctx, gtasks[0].id);
      };
      const caret = el("span", "caret", null);
      caret.title = "展开 / 折叠明细";
      caret.setAttribute("aria-label", caret.title);
      caret.appendChild(svgIcon(ICONS.chevron, 10));
      caret.onclick = (ev) => { ev.stopPropagation(); gdiv.classList.toggle("collapsed"); };
      ghead.appendChild(cb);
      ghead.appendChild(caret);

      const first = gtasks[0];
      const dirLabel = isNoise ? "测试噪音"
        : (first && first.direction === "inbound" ? "Codex→Hermes" : "Hermes→Codex");
      const dirIcon = el("span", "dirchip " + (isNoise ? "noise"
        : (first && first.direction === "inbound" ? "inb" : "outb")), null);
      dirIcon.title = dirLabel;
      dirIcon.setAttribute("aria-label", dirLabel);
      dirIcon.appendChild(svgIcon(isNoise ? ICONS.beaker
        : (first && first.direction === "inbound" ? ICONS.arrowL : ICONS.arrowR), 12));
      ghead.appendChild(dirIcon);

      const delAll = el("button", "del", null);
      delAll.title = isNoise ? "删除全部测试噪音" : "删除整个对话（含全部调用与 Codex 会话）";
      delAll.setAttribute("aria-label", delAll.title);
      delAll.appendChild(svgIcon(ICONS.trash, 13));
      delAll.onclick = (ev) => { ev.stopPropagation(); confirmDelete(isNoise ? "__NOISE__" : ctx, isNoise); };
      ghead.appendChild(delAll);

      const title = el("div", "gtitle", null);
      const row1 = el("div", "row1", null);
      const gn = el("span", "gname", isNoise ? "测试噪音" : ctx);
      gn.title = ctx;
      gn.setAttribute("aria-label", gn.title);
      row1.appendChild(gn);
      row1.appendChild(el("span", "badge " + (first ? (BADGE_STATES.has(first.state) ? first.state : "UNKNOWN") : "UNKNOWN"), first ? first.state : ""));
      row1.appendChild(el("span", "cnt", gtasks.length + " 次"));
      title.appendChild(row1);

      const row2 = el("div", "row2", null);
      const lastTs = gtasks.map(t => t.created_at || "").sort().pop() || "";
      row2.appendChild(el("span", "ts", fmtTime(lastTs)));
      const genMatch = String(ctx).match(/#(\\d{2})$/);
      if (genMatch) {
        const gen = el("span", "gen", "#" + genMatch[1]);
        gen.title = "第 " + genMatch[1] + " 代";
        row2.appendChild(gen);
      }
      title.appendChild(row2);
      ghead.appendChild(title);
      gdiv.appendChild(ghead);

      const sum = gtasks.map(t => t.summary || t.message_summary).find(s => s) || (isNoise ? "" : "（无简介）");
      if (isNoise) gdiv.appendChild(el("div", "gsummary noise", "连通性 / 防回声检查"));
      else {
        const gsum = el("div", "gsummary", sum || "（无简介）");
        gsum.title = sum || "（无简介）";
        gdiv.appendChild(gsum);
      }

      const sub = el("div", "gsub collapsed", null);
      for (const t of gtasks) {
        const row = el("div", "trow", null);
        const delBtn = el("button", "del", null);
        delBtn.title = "删除本次调用";
        delBtn.setAttribute("aria-label", delBtn.title);
        delBtn.appendChild(svgIcon(ICONS.trash, 12));
        delBtn.onclick = (ev) => { ev.stopPropagation(); confirmDeleteTask(t.id, t.summary || t.message_summary || ""); };
        row.appendChild(delBtn);
        row.appendChild(el("span", "badge " + (BADGE_STATES.has(t.state) ? t.state : "UNKNOWN"), t.state));
        row.appendChild(el("span", "id", (t.id || "?").slice(-10)));
        if (t.direction === "inbound" && t.gateway_state) {
          row.appendChild(el("span", "gw", "gw:" + t.gateway_state.replace("TASK_STATE_", "")));
        }
        row.appendChild(el("span", "ts", fmtTime(t.created_at)));
        sub.appendChild(row);
      }
      gdiv.appendChild(sub);

      ghead.onclick = () => {
        if (multiMode) { cb.click(); return; }
        if (isNoise) {
          $("conn").textContent = "🧪 测试噪音";
          $("connmeta").textContent = "连通性 / 防回声检查，可整组删除";
          currentCtx = null; current = null;
          renderEmpty("");
          return;
        }
        gdiv.classList.toggle("collapsed");
        selectContext(ctx, gtasks[0].id);
      };
      box.appendChild(gdiv);
    }
    if (!current) renderOverview(allTasks);
  } catch (e) {
    console.error("refreshTasks error", e);
  }
}

function selectContext(ctx, taskId) {
  currentCtx = ctx; current = taskId; lastSeq = -1; polling = false;
  $("conn").textContent = "对话 " + ctx;
  $("connmeta").textContent = "";
  loadConversation(taskId);
}

async function loadConversation(id) {
  if (convTimer) { clearInterval(convTimer); convTimer = null; }
  const box = $("events");
  try {
    const r = await fetch("/tasks/" + encodeURIComponent(id) + "/conversation");
    const d = await r.json();
    if (id !== current) return;
    renderConversation(d.messages, d.source);
    $("connmeta").textContent = (d.source || "") + (d.taskId ? " · " + d.taskId : "");
    const task = allTasks.find(t => t.id === id);
    if (task && task.state === "WORKING") {
      convTimer = setInterval(() => {
        if (!current || current !== id) { clearInterval(convTimer); convTimer = null; return; }
        loadConversation(id);
      }, 3000);
    }
  } catch (e) {
    if (id === current) renderEmpty("对话加载失败: " + e);
  }
}

// ============ 多选批量删除 ============
let multiMode = false;
const selectedCtxs = new Set();
let currentGroups = [];

function toggleMultiSelect() {
  multiMode = !multiMode;
  $("btn-multi").classList.toggle("on", multiMode);
  $("multi-bar").classList.toggle("hidden", !multiMode);
  if (multiMode) {
    refreshTasks();               // 重渲染显示 checkbox
  } else {
    selectedCtxs.clear(); refreshTasks();
  }
}
function toggleSelectAll() {
  const ctxs = new Set([...selectedCtxs]);
  const all = currentGroups ? currentGroups.map(g => g.ctx) : [];
  const allSelected = all.length > 0 && all.every(c => selectedCtxs.has(c));
  if (allSelected) selectedCtxs.clear();
  else all.forEach(c => selectedCtxs.add(c));
  refreshTasks();
}
function updateMultiBar() {
  $("mb-count").textContent = selectedCtxs.size;
}
function confirmMultiDelete() {
  if (!selectedCtxs.size) return;
  const n = selectedCtxs.size;
  $("dlg-title").textContent = "批量删除 " + n + " 个对话？";
  $("dlg-desc").textContent = "将删除选中的 " + n + " 个对话及其全部调用记录与 Codex 会话历史。";
  $("dlg-warn").textContent = "此操作不可恢复！";
  dlgAction = async () => {
    const headers = {};
    const tok = window.__TOKEN || TOKEN;
    if (tok) headers["Authorization"] = "Bearer " + tok;
    // 收集选中组的所有任务
    const targets = [];
    for (const ctx of selectedCtxs) {
      if (ctx === "__NOISE__") targets.push(...allTasks.filter(t => t.noise));
      else targets.push(...allTasks.filter(t => (t.contextId || "") === ctx));
    }
    let ok = 0;
    for (const t of targets) {
      try { const r = await fetch("/tasks/" + encodeURIComponent(t.id), { method: "DELETE", headers }); if (r.ok) ok++; } catch (e) {}
    }
    $("conn").textContent = "已删除 " + ok + "/" + targets.length + " 条";
    selectedCtxs.clear();
    toggleMultiSelect();
    refreshTasks();
  };
  $("confirmDlg").showModal();
}

// 确认对话框（替代 confirm）
let dlgAction = null;
function confirmDelete(ctx, isNoise) {
  $("dlg-title").textContent = isNoise ? "删除全部测试噪音？" : "删除整个对话？";
  $("dlg-desc").textContent = isNoise ? "将删除全部 " + allTasks.filter(t=>t.noise).length + " 条测试噪音记录（连通性 / 防回声检查）。" : "将删除对话 " + ctx + " 及其全部调用记录与 Codex 会话历史。";
  $("dlg-warn").textContent = "此操作不可恢复！";
  dlgAction = () => doDeleteByFilter(isNoise);
  $("confirmDlg").showModal();
}
function confirmDeleteTask(id, summary) {
  $("dlg-title").textContent = "删除本次调用？";
  $("dlg-desc").textContent = "任务 " + id + (summary ? " · " + summary : "") + "。";
  $("dlg-warn").textContent = "删除后无法恢复（含 Codex 会话历史）。";
  dlgAction = () => doDeleteOne(id);
  $("confirmDlg").showModal();
}
$("dlg-cancel").onclick = () => $("confirmDlg").close();
$("dlg-ok").onclick = async () => {
  const fn = dlgAction; $("confirmDlg").close(); dlgAction = null;
  if (fn) await fn();
};
async function doDeleteByFilter(isNoise) {
  const headers = {};
  const tok = window.__TOKEN || TOKEN;
  if (tok) headers["Authorization"] = "Bearer " + tok;
  const targets = allTasks.filter(t => isNoise ? t.noise : (t.contextId || "") === currentCtx);
  let okCount = 0;
  for (const t of targets) {
    try {
      const r = await fetch("/tasks/" + encodeURIComponent(t.id), { method: "DELETE", headers });
      if (r.ok) okCount++;
    } catch (e) {}
  }
  $("conn").textContent = "已删除 " + okCount + "/" + targets.length + " 条";
  refreshTasks();
}
async function doDeleteOne(id) {
  const headers = {};
  const tok = window.__TOKEN || TOKEN;
  if (tok) headers["Authorization"] = "Bearer " + tok;
  try {
    const r = await fetch("/tasks/" + encodeURIComponent(id), { method: "DELETE", headers });
    $("conn").textContent = r.ok ? "已删除" : "删除失败";
  } catch (e) { $("conn").textContent = "删除失败: " + e; }
  refreshTasks();
}

// 启动时若 TOKEN 为空，自动向后端要 token（供 DELETE 鉴权）
(async function () {
  if (!TOKEN) {
    try {
      const r = await fetch("/monitor-token");
      const d = await r.json();
      if (d && d.token) window.__TOKEN = d.token;
    } catch (e) { console.warn("获取监控 token 失败", e); }
  }
})();

// 连接状态 + 手动刷新 + 最后刷新时间
function setConn(ok) {
  const dot = $("conn-dot"), txt = $("conn-text");
  dot.className = "conn-state " + (ok ? "ok" : "err");
  txt.textContent = ok ? "Connected" : "Offline";
}
function fmtClock() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}
$("btn-refresh").addEventListener("click", () => { setConn(true); $("last-refresh").textContent = fmtClock(); refreshTasks(); });

// 定时刷新（更新连接状态与刷新时间）
setInterval(() => { refreshTasks(); setConn(true); $("last-refresh").textContent = fmtClock(); }, 3000);
refreshTasks();
setConn(true);
$("last-refresh").textContent = fmtClock();
</script>
</body>
</html>
"""


# A2A v1 JSON-RPC error code for unsupported content types.
CONTENT_TYPE_NOT_SUPPORTED = -32005

_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'`\u4e00-\u9fff]*")
_UNIX_PATH_RE = re.compile(r"(?:/[\w.+-]+){3,}")
_ALLOWED_CARD_HOST_RE = re.compile(
    r"^(?:(?:127\.0\.0\.1|localhost|\[::1\])(?::\d{1,5})?)$",
    re.IGNORECASE,
)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def text_part(text: str) -> dict[str, Any]:
    return {"text": text, "mediaType": "text/plain"}


def agent_message(text: str, context_id: str) -> dict[str, Any]:
    return {
        "messageId": f"msg-{uuid.uuid4().hex}",
        "contextId": context_id,
        "role": "ROLE_AGENT",
        "parts": [text_part(text)],
    }


def extract_message_text(params: dict[str, Any]) -> str:
    message = params.get("message") or {}
    parts = message.get("parts") or []
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        value = part.get("text")
        if isinstance(value, str):
            chunks.append(value)
            continue
        nested = part.get("root")
        if isinstance(nested, dict) and isinstance(nested.get("text"), str):
            chunks.append(nested["text"])
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def resolve_context_id(params: dict[str, Any]) -> str:
    message = params.get("message") or {}
    return str(
        message.get("contextId")
        or message.get("context_id")
        or params.get("contextId")
        or params.get("context_id")
        or f"ctx-{uuid.uuid4().hex}"
    )


def wants_immediate_return(params: dict[str, Any]) -> bool:
    """Return immediately only when the caller explicitly opted in."""
    config = params.get("configuration") or params.get("config") or {}
    return bool(config.get("returnImmediately") or config.get("return_immediately"))


class ContentTypeNotSupportedError(Exception):
    """Raised when a message contains non-text parts we cannot handle."""


def ensure_text_only_message(params: dict[str, Any]) -> None:
    """Reject any non-text part with the A2A ContentTypeNotSupported error."""
    message = params.get("message") or {}
    parts = message.get("parts") or []
    for part in parts:
        if not isinstance(part, dict):
            continue
        media = str(part.get("mediaType") or "text/plain")
        if not media.startswith("text/"):
            raise ContentTypeNotSupportedError(
                f"Unsupported part mediaType '{media}'; only text parts are supported"
            )
        has_text = (
            isinstance(part.get("text"), str)
            and bool(part["text"].strip())
        ) or (
            isinstance(part.get("root"), dict)
            and isinstance(part["root"].get("text"), str)
            and bool(part["root"]["text"].strip())
        )
        if not has_text:
            raise ContentTypeNotSupportedError("text part without text content")


def sanitize_error(message: str) -> str:
    """Strip local filesystem paths from user-visible error messages."""
    if not message:
        return message
    text = _WINDOWS_PATH_RE.sub("[path]", message)
    text = _UNIX_PATH_RE.sub("[path]", text)
    return text


def find_codex_executable(explicit: str | None = None) -> str:
    candidates = [
        explicit,
        os.environ.get("CODEX_CLI_PATH"),
        shutil.which("codex.exe"),
        shutil.which("codex.cmd"),
        shutil.which("codex"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate).resolve())
    raise FileNotFoundError("Codex CLI not found; set CODEX_CLI_PATH or pass --codex")


def parse_codex_jsonl(output: str) -> tuple[str | None, str]:
    """Extract the session id and final agent text from Codex JSONL output."""
    session_id: str | None = None
    final_message = ""
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            session_id = event["thread_id"]
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            final_message = item["text"]
    return session_id, final_message


def _iso_to_epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class SessionStore:
    """Codex 会话映射 + 工作线（workstream）注册表。

    ``sessions``: context_id -> session_id（旧格式，兼容）
    ``workstreams``: 逻辑工作线名 -> 元数据（代次/会话/健康状态）

    工作线是"调用方传的逻辑名"，代次由桥自动管理：
    resolve_workstream("demo-project") -> 返回当前活跃代次的 context_id，
    无则建 #01；健康检查触发 rotate 时自动建 #N+1 并归档旧的。
    """

    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file
        self.lock = threading.RLock()
        self.sessions: dict[str, str] = {}
        self.workstreams: dict[str, dict] = {}
        self._ws_locks: dict[str, threading.RLock] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
            for entry in payload.get("sessions", []):
                context_id = entry.get("context_id")
                session_id = entry.get("session_id")
                if isinstance(context_id, str) and isinstance(session_id, str):
                    self.sessions[context_id] = session_id
            for name, meta in (payload.get("workstreams") or {}).items():
                if isinstance(meta, dict):
                    self.workstreams[name] = meta
        except Exception:
            logging.exception("Could not load persisted Codex sessions")

    def _persist_locked(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessions": [
                {"context_id": context_id, "session_id": session_id}
                for context_id, session_id in self.sessions.items()
            ],
            "workstreams": self.workstreams,
        }
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def get(self, context_id: str) -> str | None:
        with self.lock:
            return self.sessions.get(context_id)

    def context_for_session(self, session_id: str) -> str | None:
        """按 session_id 反查 context_id（删除任务时判断会话归属）。"""
        with self.lock:
            for context_id, sid in self.sessions.items():
                if sid == session_id:
                    return context_id
        return None

    def remove(self, context_id: str) -> None:
        with self.lock:
            if context_id in self.sessions:
                del self.sessions[context_id]
                self._persist_locked()

    def set(self, context_id: str, session_id: str) -> None:
        with self.lock:
            self.sessions[context_id] = session_id
            self._persist_locked()

    # ------------------------------------------------------------------
    # 工作线（workstream）注册表
    # ------------------------------------------------------------------

    def ws_lock(self, name: str) -> threading.RLock:
        """per-workstream 锁：同一工作线串行，防并行消息交叉。"""
        with self.lock:
            if name not in self._ws_locks:
                self._ws_locks[name] = threading.RLock()
            return self._ws_locks[name]

    def resolve_workstream(
        self,
        name: str,
        *,
        profile: str = "",
        workspace: str = "",
        model: str = "",
    ) -> str:
        """返回工作线当前活跃代次的 context_id；无则创建 #01。

        context_id 形如 ``ctx-<name>#<NN>``。调用方只传逻辑名，
        代次由这里管理。旧 workstream 已 closed/archived 时开新代次。
        """
        with self.lock:
            meta = self.workstreams.get(name)
            now = utc_timestamp()
            if meta and meta.get("status") in ("active", "warning"):
                # 自动轮换：启用开关且超硬阈值时归档旧代次、开新代次
                if WS_AUTO_ROTATE:
                    rotate_reason = self.should_rotate(name)
                    if rotate_reason:
                        meta["status"] = "rotate"
                        meta["rotate_reason"] = rotate_reason
                        meta["closed_at"] = now
                        # 归档旧代次后走下方新建逻辑
                        meta = None
                if meta is not None:
                    # 健康检查：warning 仍复用（默认 warning-only）
                    health = self.check_health(name)
                    context_id = meta["context_id"]
                    meta["last_used_at"] = now
                    self._persist_locked()
                    return context_id
            # 新建代次：找下一个编号
            gen = 1
            if meta:
                gen = int(meta.get("generation", 0)) + 1
            context_id = f"ctx-{name}#{gen:02d}"
            self.workstreams[name] = {
                "generation": gen,
                "context_id": context_id,
                "session_id": self.sessions.get(context_id, ""),
                "profile": profile,
                "workspace": workspace,
                "model": model,
                "created_at": now,
                "last_used_at": now,
                "last_success_at": "",
                "message_count": 0,
                "estimated_tokens": 0,
                "file_size": 0,
                "status": "active",
                "rotate_reason": "",
                "closed_at": "",
            }
            self._persist_locked()
            return context_id

    def touch_workstream(self, name: str, *, session_id: str = "", message_count: int = 0,
                         estimated_tokens: int = 0, file_size: int = 0) -> None:
        """任务完成后更新工作线元数据（会话 id、消息数、token 估算）。"""
        with self.lock:
            meta = self.workstreams.get(name)
            if not meta:
                return
            now = utc_timestamp()
            meta["last_used_at"] = now
            meta["last_success_at"] = now
            if session_id:
                meta["session_id"] = session_id
                self.sessions[meta["context_id"]] = session_id
            if message_count:
                meta["message_count"] = message_count
            if estimated_tokens:
                meta["estimated_tokens"] = estimated_tokens
            if file_size:
                meta["file_size"] = file_size
            self._persist_locked()

    def set_workstream_status(self, name: str, status: str, reason: str = "") -> None:
        """标记工作线状态：warning / rotate / closed / archived。"""
        with self.lock:
            meta = self.workstreams.get(name)
            if not meta:
                return
            meta["status"] = status
            if reason:
                meta["rotate_reason"] = reason
            if status in ("closed", "archived", "rotate"):
                meta["closed_at"] = utc_timestamp()
            self._persist_locked()

    def close_workstream(self, name: str, reason: str = "explicit") -> None:
        """关闭工作线：标记 closed，保留归档期（不删映射）。"""
        self.set_workstream_status(name, "closed", reason)

    def list_workstreams(self) -> list[dict]:
        with self.lock:
            return [dict(meta, name=name) for name, meta in self.workstreams.items()]

    def resolve_ephemeral(self, *, workspace: str = "", profile: str = "") -> str:
        """自动兜底：按 workspace+profile+时间窗生成短期工作线（默认关闭）。

        未传工作线名时：30 分钟内有活跃的同一 workspace/profile 临时线则复用，
        否则新建。标记 status=ephemeral，便于监控页识别和清理。
        """
        if not WS_AUTO_EPHEMERAL:
            return ""
        key = f"auto:{workspace}:{profile}"
        now = utc_timestamp()
        with self.lock:
            meta = self.workstreams.get(key)
            if meta and meta.get("status") in ("active", "ephemeral"):
                try:
                    from datetime import datetime
                    last_dt = datetime.fromisoformat((meta.get("last_used_at") or "").replace("Z", "+00:00"))
                    age = (datetime.now(last_dt.tzinfo) - last_dt).total_seconds()
                    if age < WS_EPHEMERAL_WINDOW_SECONDS:
                        meta["last_used_at"] = now
                        self._persist_locked()
                        return meta["context_id"]
                except (ValueError, TypeError):
                    pass
            gen = 1
            if meta:
                gen = int(meta.get("generation", 0)) + 1
            context_id = f"ctx-{key}#{gen:02d}"
            self.workstreams[key] = {
                "generation": gen, "context_id": context_id, "session_id": "",
                "profile": profile, "workspace": workspace, "model": "",
                "created_at": now, "last_used_at": now, "last_success_at": "",
                "message_count": 0, "estimated_tokens": 0, "file_size": 0,
                "status": "ephemeral", "rotate_reason": "auto", "closed_at": "",
            }
            self._persist_locked()
            return context_id

    def archive_workstream(self, name: str, reason: str = "cleanup") -> None:
        """归档已关闭/轮换的工作线（保留映射与原因，可回溯）。"""
        self.set_workstream_status(name, "archived", reason)

    def cleanup_archived(self, max_age_days: int = 30) -> int:
        """清理超过保留期的 archived 工作线（dry-run 由调用方控制）。

        返回移除的工作线数量。默认保留 30 天。
        """
        import datetime as _dt
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=max_age_days)
        removed = 0
        with self.lock:
            for name in list(self.workstreams.keys()):
                meta = self.workstreams[name]
                if meta.get("status") != "archived":
                    continue
                closed = meta.get("closed_at", "")
                try:
                    closed_dt = datetime.fromisoformat(closed.replace("Z", "+00:00"))
                    if closed_dt < cutoff:
                        del self.workstreams[name]
                        removed += 1
                except (ValueError, TypeError):
                    continue
            if removed:
                self._persist_locked()
        return removed

    def _health_reasons(self, meta: dict) -> list[str]:
        """计算健康告警原因（warning + rotate 阈值）。"""
        reasons = []
        tokens = meta.get("estimated_tokens", 0)
        if tokens and tokens > WS_ROTATE_TOKEN_BUDGET:
            reasons.append(f"tokens={tokens}>=ROTATE")
        elif tokens and tokens > WS_WARN_TOKEN_BUDGET:
            reasons.append(f"tokens={tokens}")
        msgs = meta.get("message_count", 0)
        if msgs and msgs > WS_ROTATE_MESSAGES:
            reasons.append(f"msgs={msgs}>=ROTATE")
        elif msgs and msgs > WS_WARN_MESSAGES:
            reasons.append(f"msgs={msgs}")
        size = meta.get("file_size", 0)
        if size and size > WS_ROTATE_FILE_BYTES:
            reasons.append(f"size={size//1024}KB>=ROTATE")
        elif size and size > WS_WARN_FILE_BYTES:
            reasons.append(f"size={size//1024}KB")
        last = meta.get("last_used_at", "")
        if last:
            try:
                from datetime import datetime
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                idle_days = (datetime.now(last_dt.tzinfo) - last_dt).days
                if idle_days > WS_ROTATE_IDLE_DAYS:
                    reasons.append(f"idle={idle_days}d>=ROTATE")
                elif idle_days > WS_WARN_IDLE_DAYS:
                    reasons.append(f"idle={idle_days}d")
            except ValueError:
                pass
        return reasons

    def should_rotate(self, name: str) -> str | None:
        """返回 rotate 原因；不需要轮换返回 None。"""
        with self.lock:
            meta = self.workstreams.get(name)
            if not meta or meta.get("status") in ("closed", "archived", "rotate"):
                return None
            for reason in self._health_reasons(meta):
                if ">=ROTATE" in reason:
                    return reason
            return None

    def check_health(self, name: str) -> str:
        """健康检查：返回 'active' 或 'warning'（带原因）。

        阈值：token 预算 60%、消息 50、空闲 5 天、文件 2MB。
        warning 仅标记；rotate 由 should_rotate + WS_AUTO_ROTATE 决定。
        """
        with self.lock:
            meta = self.workstreams.get(name)
            if not meta or meta.get("status") in ("closed", "archived", "rotate"):
                return meta.get("status", "active") if meta else "active"
            reasons = self._health_reasons(meta)
            if reasons:
                meta["status"] = "warning"
                meta["rotate_reason"] = "; ".join(reasons)
                self._persist_locked()
                return "warning"
            if meta.get("status") == "warning":
                meta["status"] = "active"
                meta["rotate_reason"] = ""
                self._persist_locked()
            return "active"




# ---------------------------------------------------------------------------
# Codex 会话文件解析（历史对话展示 / 删除时定位文件）
# ---------------------------------------------------------------------------

_SESSION_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# session_id -> (path, mtime_ns, size)；rglob 只在缓存未命中时执行一次
_session_file_cache: dict[str, tuple[Path, int, int]] = {}
# 全量索引：session_id -> path（一次 rglob 建全表，供列表过滤）
_session_index: dict[str, Path] = {}
_session_index_key: tuple[int, int] | None = None


def _codex_sessions_root() -> Path:
    return Path(os.path.expanduser("~")) / ".codex" / "sessions"


def is_noise_message(message: str) -> bool:
    """判断 inbound 调用是否为测试噪音（连通性/防回声检查等）。

    测试调用特征：message 极短且无业务内容（hi/ping/test 等），
    或含防回声泄漏标记（TOP-SECRET-MARKER-* 之类唯一标记）。
    这类调用无业务价值，监控页单独归组便于批量清理。
    """
    t = (message or "").strip()
    if not t:
        return True
    low = t.lower()
    if low in ("hi", "hello", "ping", "test", "测试", "收到", "回复两个字：收到"):
        return True
    if len(t) <= 12 and not any(c.isalnum() for c in t) is False:
        # 极短且像随机串的（如标记）
        pass
    if "top-secret-marker" in low or "secret-marker" in low or "echo" in low:
        return True
    return False


def estimate_session_stats(session_id: str) -> dict:
    """从 Codex 会话文件估算消息数/token/文件大小（健康检查用）。

    字符数->token 粗估（中文约 1.5 字符/token，通用按 4 字符/token），
    只做趋势和阈值判断，不精确计费。
    """
    path = find_codex_session_file(session_id)
    stats = {"message_count": 0, "estimated_tokens": 0, "file_size": 0, "exists": False}
    if path is None:
        return stats
    try:
        stats["file_size"] = path.stat().st_size
        stats["exists"] = True
    except OSError:
        return stats
    count = 0
    chars = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                payload = obj.get("payload") or {}
                if obj.get("type") == "response_item" and payload.get("role") in ("user", "assistant", "tool"):
                    count += 1
                    for content in payload.get("content") or []:
                        if isinstance(content, dict) and content.get("text"):
                            chars += len(content["text"])
    except OSError:
        pass
    stats["message_count"] = count
    stats["estimated_tokens"] = int(chars / 4) + count * 8
    return stats


def _invalidate_session_cache(session_id: str) -> None:
    _session_file_cache.pop(session_id, None)


def session_index() -> dict[str, Path]:
    """一次 rglob 扫描建全量 session_id -> path 索引（带目录 mtime/size 缓存）。

    列表过滤用：避免每个任务都单独 rglob 一次。
    """
    global _session_index, _session_index_key
    root = _codex_sessions_root()
    if not root.exists():
        return {}
    try:
        stat = root.stat()
        # 缓存键 = root 自身签名 + 子目录树签名（Codex 按 YYYY/MM/DD 分目录，
        # 新会话只改子目录 mtime，root 不变——必须把子目录签名纳入）
        key = (stat.st_mtime_ns, stat.st_size, _dir_tree_sig(root))
    except OSError:
        return {}
    if _session_index_key == key:
        return _session_index
    index: dict[str, Path] = {}
    for candidate in root.rglob("rollout-*.jsonl"):
        stem = candidate.stem
        # rollout-<ts>-<uuid>：取最后一个 UUID 段做 session_id
        m = re.search(r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", stem)
        if m:
            index[m.group(1)] = candidate
    _session_index = index
    _session_index_key = key
    return index


def _dir_tree_sig(root: Path) -> tuple:
    """递归目录树签名：所有子目录 (name, mtime_ns) 的排序元组。

    Codex 按 YYYY/MM/DD 分层存放会话文件，新会话写入时只有深层目录
    的 mtime 变化。用它做缓存失效依据，避免索引陈旧。
    """
    sig = []
    try:
        for dirpath, dirnames, _ in os.walk(root):
            for d in sorted(dirnames):
                dp = Path(dirpath) / d
                try:
                    st = dp.stat()
                    sig.append((str(dp.relative_to(root)), st.st_mtime_ns))
                except OSError:
                    continue
    except OSError:
        return ()
    return tuple(sorted(sig))


def find_codex_session_file(session_id: str) -> Path | None:
    """在 ~/.codex/sessions/ 下按 session_id 定位 rollout jsonl 文件。

    文件名形如 rollout-<ts>-<session_id>.jsonl。要求 session_id 是完整
    UUID、basename 精确匹配、路径解析后位于 sessions root 之下且为普通
    文件。路径按 (mtime, size) 缓存，避免每次请求全量 rglob。
    """
    if not session_id or not _SESSION_UUID_RE.match(session_id):
        return None
    cached = _session_file_cache.get(session_id)
    if cached is not None:
        path, mtime, size = cached
        try:
            stat = path.stat()
            if stat.st_mtime_ns == mtime and stat.st_size == size:
                return path
        except OSError:
            pass
        _session_file_cache.pop(session_id, None)
    # 优先用全量索引（一次扫描）
    indexed = session_index().get(session_id)
    if indexed is not None:
        try:
            stat = indexed.stat()
        except OSError:
            pass
        else:
            _session_file_cache[session_id] = (indexed, stat.st_mtime_ns, stat.st_size)
            return indexed
    root = _codex_sessions_root()
    if not root.exists():
        return None
    root_resolved = root.resolve()
    for candidate in root.rglob("rollout-*.jsonl"):
        if candidate.stem.endswith(session_id):
            try:
                resolved = candidate.resolve()
                # 必须位于 sessions root 下，且是普通文件，且 basename 精确匹配
                if resolved.parent != root_resolved and not resolved.is_relative_to(root_resolved):
                    continue
                if not resolved.is_file():
                    continue
                # 精确匹配：文件名以完整 session_id 结尾（防 019f...-x 误配）
                if not resolved.stem.endswith(session_id) or len(resolved.stem) < len("rollout-") + len(session_id):
                    continue
            except (OSError, ValueError):
                continue
            try:
                stat = resolved.stat()
            except OSError:
                continue
            _session_file_cache[session_id] = (resolved, stat.st_mtime_ns, stat.st_size)
            return resolved
    return None


def parse_codex_conversation(session_id: str, max_messages: int = 200) -> list[dict]:
    """解析 Codex 会话文件，返回按时间排序的对话消息列表。

    每条: {role: user|assistant|tool, ts, text}。跳过系统注入内容
    （AGENTS.md、app-context、skills 列表等）。解析失败返回空列表。
    """
    path = find_codex_session_file(session_id)
    if path is None:
        return []
    messages: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "response_item":
                    continue
                payload = obj.get("payload") or {}
                ptype = payload.get("type") or obj.get("type")
                role = payload.get("role")
                ts = obj.get("timestamp") or payload.get("timestamp") or ""
                # 工具事件：function_call / function_call_output / custom_tool_call /
                # custom_tool_call_output / command_execution 等，role 常为空
                if role in ("tool", "function") or ptype in (
                    "function_call", "function_call_output", "custom_tool_call",
                    "custom_tool_call_output", "command_execution", "mcp_tool_call",
                ):
                    name = payload.get("name") or payload.get("tool_name") or payload.get("command") or ptype
                    detail = payload.get("arguments") or payload.get("output") or payload.get("aggregated_output") or ""
                    if isinstance(detail, (dict, list)):
                        detail = json.dumps(detail, ensure_ascii=False)
                    text = f"调用 {name}" + (f" | {str(detail)[:2000]}" if detail else "")
                    messages.append({"role": "tool", "ts": ts, "text": text[:20000]})
                    continue
                if role not in ("user", "assistant"):
                    continue
                for content in payload.get("content") or []:
                    if not isinstance(content, dict):
                        continue
                    ctype = content.get("type")
                    text = ""
                    if ctype in ("input_text", "output_text"):
                        text = content.get("text", "")
                    elif ctype in ("tool_use", "tool_result"):
                        text = json.dumps(content, ensure_ascii=False)[:2000]
                    if not text or not text.strip():
                        continue
                    t = text.strip()
                    if t.startswith((
                        "# AGENTS.md", "<app-context>", "<INSTRUCTIONS>", "You are `/root`",
                        "<multi_agent_mode>", "<skills_instructions>", "<recommended_plugins>",
                        "<environment_context>", "# Codex desktop context",
                    )):
                        continue
                    # 桥注入的系统前缀：若整条消息以桥系统提示开头（含 "Hermes task:" 标记），
                    # 只保留标记之后的实际任务内容；正文里出现该标记则不动（防误伤）。
                    if role == "user":
                        bridge_prefix = (
                            "You are Codex receiving a task from Hermes over a local A2A bridge"
                        )
                        marker = "Hermes task:"
                        if t.startswith(bridge_prefix) and marker in t:
                            t = t.split(marker, 1)[1].strip()
                    messages.append({
                        "role": role,
                        "ts": ts,
                        "text": t[:20000],
                    })
    except OSError:
        return []
    return messages[-max_messages:]


class TaskStore:
    """Task metadata store.

    Large result text is kept in ``state_dir/results/<task_id>.txt`` while
    ``tasks.json`` holds only metadata. ``get()`` materializes the result on
    demand instead of deep-copying the whole in-memory payload.
    """

    def __init__(self, state_file: Path, max_tasks: int = 100) -> None:
        self.state_file = state_file
        self.results_dir = state_file.parent / "results"
        self.max_tasks = max_tasks
        self.lock = threading.RLock()
        self.tasks: dict[str, dict[str, Any]] = {}
        self.events: dict[str, threading.Event] = {}
        self.event_logs: dict[str, deque] = {}  # task_id -> deque[dict] 实时事件流
        self.seen_inbound_events: set[tuple[str, str, str]] = set()  # (source, op, event) 幂等去重
        self.deleted_inbound_ops: set[str] = set()  # 已删除的 (source|op) tombstone，防复活
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
            for task in payload.get("tasks", []):
                task_id = task.get("id")
                if not task_id:
                    continue
                state = (task.get("status") or {}).get("state")
                if state not in TERMINAL_STATES:
                    # Bridge restarted while this task was WORKING: it can never
                    # resume in-process, so mark it FAILED and ask for reconciliation.
                    context_id = task.get("contextId") or f"ctx-{uuid.uuid4().hex}"
                    task["status"] = {
                        "state": "TASK_STATE_FAILED",
                        "timestamp": utc_timestamp(),
                        "message": agent_message(
                            "桥服务重启，未完成的 Codex 任务已中断。可凭 session_id 在 Codex CLI 中续跑，"
                            "并请与实际产出对账（reconcile）。",
                            context_id,
                        ),
                    }
                    task["finished_at"] = utc_timestamp()
                self._normalize_locked(task)
                self.tasks[task_id] = task
                event = threading.Event()
                event.set()
                self.events[task_id] = event
                self.event_logs.setdefault(task_id, deque(maxlen=EVENT_LOG_LINES))
        except Exception:
            logging.exception("Could not load persisted A2A tasks")

    def _persist_locked(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        ordered = list(self.tasks.values())[-self.max_tasks :]
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"tasks": ordered}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    @staticmethod
    def _extract_result_text(task: dict[str, Any]) -> str:
        status = task.get("status") or {}
        message = status.get("message") or {}
        for part in message.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                return part["text"]
        for artifact in task.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            for part in artifact.get("parts") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                    return part["text"]
        return ""

    @staticmethod
    def _strip_result_text(task: dict[str, Any]) -> None:
        status = task.get("status") or {}
        message = status.get("message") or {}
        for part in message.get("parts") or []:
            if isinstance(part, dict):
                part["text"] = ""
        for artifact in task.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            for part in artifact.get("parts") or []:
                if isinstance(part, dict):
                    part["text"] = ""

    def _normalize_locked(self, task: dict[str, Any]) -> dict[str, Any]:
        """Move completed result text out of tasks.json into results/<id>.txt."""
        if (task.get("status") or {}).get("state") == "TASK_STATE_COMPLETED":
            text = self._extract_result_text(task)
            if text:
                self._write_result(task["id"], text)
                self._strip_result_text(task)
                task["_resultExternal"] = True
        return task

    def _write_result(self, task_id: str, text: str) -> None:
        try:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.results_dir / f".{task_id}.tmp"
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.results_dir / f"{task_id}.txt")
        except OSError:
            logging.exception("Could not write result file for %s", task_id)

    def _read_result(self, task_id: str) -> str | None:
        path = self.results_dir / f"{task_id}.txt"
        try:
            if path.exists():
                return path.read_text(encoding="utf-8")
        except OSError:
            logging.exception("Could not read result file %s", path)
        return None

    def add(self, task: dict[str, Any]) -> threading.Event:
        with self.lock:
            task_id = task["id"]
            self._normalize_locked(task)
            self.tasks[task_id] = task
            event = threading.Event()
            self.events[task_id] = event
            self.event_logs.setdefault(task_id, deque(maxlen=EVENT_LOG_LINES))
            while len(self.tasks) > self.max_tasks:
                oldest = next(iter(self.tasks))
                self.tasks.pop(oldest, None)
                self.events.pop(oldest, None)
                self.event_logs.pop(oldest, None)
            self._persist_locked()
            return event

    @staticmethod
    def _can_transition(cur_state: str | None, new_state: str) -> bool:
        """状态流转合法性校验（显式规则，替代散落的隐式特判）。

        规则：
        - 无当前状态（新任务）→ 任何新状态都合法
        - 终态 → WORKING：拒绝（已完成/失败/取消的任务不允许复活为运行中）
        - 其他组合（含终态之间互转、WORKING 更新）→ 允许（保持历史行为）
        """
        if not cur_state:
            return True
        return not (cur_state in TERMINAL_STATES and new_state == "TASK_STATE_WORKING")

    def update(
        self,
        task_id: str,
        task: dict[str, Any],
        terminal: bool = False,
        if_not_state: str | None = None,
    ) -> bool:
        """Store a task update.

        ``if_not_state`` guards a race: if the stored task is already in that
        state (e.g. CANCELED), the update is rejected and False is returned.
        """
        with self.lock:
            current = self.tasks.get(task_id)
            if current is None:
                return False
            if if_not_state and (current.get("status") or {}).get("state") == if_not_state:
                return False
            self._normalize_locked(task)
            if terminal:
                task.setdefault("finished_at", utc_timestamp())
            if "created_at" not in task and "created_at" in current:
                # 终态更新常只带 status/artifacts，保留原始创建时间供排序
                task["created_at"] = current["created_at"]
            if "summary" not in task and "summary" in current:
                task["summary"] = current["summary"]
            self.tasks[task_id] = task
            self._persist_locked()
            if terminal:
                self.events.setdefault(task_id, threading.Event()).set()
            return True

    def _materialize_locked(self, task: dict[str, Any]) -> dict[str, Any]:
        """Shallow-reconstruct a task, injecting result text from disk."""
        result = dict(task)
        result.pop("_resultExternal", None)
        status = dict(result.get("status") or {})
        result["status"] = status
        message = status.get("message")
        if isinstance(message, dict):
            message = dict(message)
            message["parts"] = [dict(p) for p in message.get("parts") or [] if isinstance(p, dict)]
            status["message"] = message
        artifacts = result.get("artifacts")
        if isinstance(artifacts, list):
            result["artifacts"] = [dict(a) for a in artifacts if isinstance(a, dict)]
        if task.get("_resultExternal"):
            text = self._read_result(task["id"])
            if text is not None:
                parts = message.get("parts") if isinstance(message, dict) else []
                for part in parts:
                    if isinstance(part, dict):
                        part["text"] = text
                for artifact in result.get("artifacts") or []:
                    if not isinstance(artifact, dict):
                        continue
                    for part in artifact.get("parts") or []:
                        if isinstance(part, dict):
                            part["text"] = text
        return result

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            return self._materialize_locked(task)

    def append_event(self, task_id: str, event: dict[str, Any]) -> None:
        """Append a structured progress event to the task's live log.

        Events carry a monotonically increasing ``seq`` used by the UI's
        incremental polling (``events_since``). Never raises — the live log
        is best-effort and must not disturb task execution.
        """
        try:
            with self.lock:
                if task_id not in self.tasks:
                    # 未知或已淘汰任务：拒绝写入，避免孤儿日志无限增长
                    return
                log = self.event_logs.setdefault(task_id, deque(maxlen=EVENT_LOG_LINES))
                seq = (log[-1]["seq"] + 1) if log else 0
                entry = {**event, "seq": seq}
                log.append(entry)
        except Exception:
            logging.exception("Could not append event for %s", task_id)

    def events_since(self, task_id: str, after: int = -1, limit: int = 500) -> tuple[list[dict[str, Any]], int]:
        """Return events with ``seq > after`` and the latest seq (or -1 if none).

        Used by the monitoring UI for incremental polling; safe to call
        concurrently with ``append_event``.
        """
        with self.lock:
            log = self.event_logs.get(task_id)
            if not log:
                return [], -1
            latest = log[-1]["seq"] if log else -1
            return [e for e in log if e["seq"] > after][-limit:], latest

    def wait(self, task_id: str, timeout: float) -> bool:
        with self.lock:
            event = self.events.get(task_id)
        return bool(event and event.wait(timeout))

    def attach_process(self, task_id: str, process: subprocess.Popen[str]) -> None:
        with self.lock:
            self.processes[task_id] = process

    def detach_process(self, task_id: str) -> None:
        with self.lock:
            self.processes.pop(task_id, None)

    def is_canceled(self, task_id: str) -> bool:
        with self.lock:
            task = self.tasks.get(task_id)
            return bool(task and (task.get("status") or {}).get("state") == "TASK_STATE_CANCELED")

    def active_count(self) -> int:
        """统计占用 Codex 进程槽位的任务数。

        只统计 outbound（direction != inbound）的 WORKING 任务——
        inbound 是反向链路上报（Codex->Hermes），不占 Codex 进程，
        若计入会导致僵尸 WORKING 占满队列、新任务全被拒。
        """
        with self.lock:
            return sum(
                1
                for task in self.tasks.values()
                if (task.get("status") or {}).get("state") == "TASK_STATE_WORKING"
                and task.get("direction") != "inbound"
            )

    def cleanup_stuck_inbound(self, max_age: float = INBOUND_STUCK_TIMEOUT) -> int:
        """清理卡死的 inbound 任务：started 后超过 max_age 秒仍 WORKING
        （MCP 上报方崩溃/超时未发 finished）→ 标记 FAILED。

        返回清理数量。返回 (n, affected_ids)。
        """
        import time as _time
        affected = []
        now = _time.time()
        with self.lock:
            for task_id, task in self.tasks.items():
                if task.get("direction") != "inbound":
                    continue
                if (task.get("status") or {}).get("state") != "TASK_STATE_WORKING":
                    continue
                observed = task.get("last_observed_at") or task.get("created_at") or ""
                try:
                    from datetime import datetime
                    obs = datetime.fromisoformat(observed.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    continue
                if now - obs > max_age:
                    context_id = task.get("contextId") or ""
                    task["status"] = {
                        "state": "TASK_STATE_FAILED",
                        "timestamp": utc_timestamp(),
                        "message": agent_message(
                            f"反向调用（Codex→Hermes）上报后 {int(max_age)}s 未收到终态，"
                            "视为卡死已标记失败（MCP 上报方可能崩溃）。",
                            context_id,
                        ),
                    }
                    task["finished_at"] = utc_timestamp()
                    affected.append(task_id)
            if affected:
                self._persist_locked()
        return affected

    def update_heartbeat(self, task_id: str) -> bool:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task or (task.get("status") or {}).get("state") in TERMINAL_STATES:
                return False
            task["last_heartbeat"] = utc_timestamp()
            self._persist_locked()
            return True

    def query(self, states: list[str] | None = None) -> list[dict[str, Any]]:
        allowed: set[str] | None = None
        if states:
            allowed = {s for s in states if isinstance(s, str)}
        with self.lock:
            ordered = []
            for task in self.tasks.values():
                state = (task.get("status") or {}).get("state")
                if allowed and state not in allowed:
                    continue
                ordered.append(self._materialize_locked(task))
            ordered.sort(key=lambda t: (t.get("created_at") or ""), reverse=True)
            return ordered[:MAX_QUERY_RESULTS]

    def query_brief(self, limit: int = MAX_QUERY_RESULTS) -> list[dict[str, Any]]:
        """元数据快照（不含结果文本、不含 session_id），供监控 UI 轮询。

        相比 ``query()`` 不做结果文件 materialize——监控页每 3 秒轮询一次，
        避免反复读取最多 100 个结果文件造成磁盘 IO 与锁竞争。
        """
        with self.lock:
            ordered = []
            for task in self.tasks.values():
                status = task.get("status") or {}
                ordered.append({
                    "id": task.get("id"),
                    "state": status.get("state", "").replace("TASK_STATE_", ""),
                    "contextId": task.get("contextId"),
                    "created_at": task.get("created_at"),
                    "finished_at": task.get("finished_at"),
                    "summary": task.get("summary", ""),
                    "direction": task.get("direction", "outbound"),
                    "source": task.get("source", ""),
                    "gateway_task_id": task.get("gateway_task_id", ""),
                    "gateway_state": task.get("gateway_state", ""),
                    "last_observed_at": task.get("last_observed_at", ""),
                    "message_summary": task.get("message_summary", ""),
                    "reply_summary": task.get("reply_summary", ""),
                    "noise": is_noise_message(task.get("message_summary", "")) if task.get("direction") == "inbound" else False,
                })
            ordered.sort(key=lambda t: (t.get("created_at") or ""), reverse=True)
            return ordered[:limit]

    def upsert_inbound(self, event: dict[str, Any]) -> tuple[str, bool]:
        """按 (source, operation_id) 幂等合并 inbound 事件，返回 (task_id, created?)。

        inbound 任务 id 为 inb-<sha256(source|op) 前 16 位>——完整操作标识的
        安全 hash，杜绝不同 operation/source 的后缀碰撞合并。
        同一 operation 的后续事件更新同一条记录。
        """
        source = event.get("source") or "hermes_mcp"
        operation_id = event.get("operation_id") or ""
        if not operation_id:
            raise ValueError("operation_id required")
        import hashlib
        task_id = "inb-" + hashlib.sha256(f"{source}|{operation_id}".encode("utf-8")).hexdigest()[:16]
        tombstone_key = f"{source}|{operation_id}"
        with self.lock:
            if tombstone_key in self.deleted_inbound_ops:
                # 该 operation 已删除：拒绝复活，返回已存在标记
                return task_id, False
        # 规范化状态 -> TASK_STATE_*（MCP 上报 WORKING/COMPLETED/...，桥内统一存储格式）
        norm = event.get("state") or ""
        state_map = {
            "WORKING": "TASK_STATE_WORKING",
            "COMPLETED": "TASK_STATE_COMPLETED",
            "FAILED": "TASK_STATE_FAILED",
            "CANCELED": "TASK_STATE_CANCELED",
            "UNKNOWN": "TASK_STATE_WORKING",  # 未知但可能仍在跑：保守当 WORKING
        }
        new_state = state_map.get(norm, "TASK_STATE_WORKING")
        with self.lock:
            existing = self.tasks.get(task_id)
            created = existing is None
            if created:
                task = {
                    "id": task_id,
                    "direction": "inbound",
                    "source": source,
                    "operation_id": operation_id,
                    "contextId": event.get("context_id") or "",
                    "profile": event.get("profile") or "",
                    "status": {"state": "TASK_STATE_WORKING", "timestamp": utc_timestamp()},
                    "created_at": event.get("observed_at") or utc_timestamp(),
                    "gateway_task_id": event.get("gateway_task_id", ""),
                    "gateway_state": event.get("gateway_state", ""),
                    "message_summary": (event.get("message_summary") or "")[:INBOUND_SUMMARY_CHARS],
                    "reply_summary": (event.get("reply_summary") or "")[:INBOUND_SUMMARY_CHARS],
                    "error_category": event.get("error_category") or "",
                    "last_observed_at": event.get("observed_at") or utc_timestamp(),
                }
                self.add(task)
            else:
                task = existing
                # 更新字段（不覆盖已有终态为 WORKING，显式流转规则）
                cur_state = (task.get("status") or {}).get("state")
                if new_state and self._can_transition(cur_state, new_state):
                    task["status"] = {"state": new_state, "timestamp": event.get("observed_at") or utc_timestamp()}
                if event.get("context_id"):
                    task["contextId"] = event["context_id"]
                if event.get("profile"):
                    task["profile"] = event["profile"]
                if event.get("gateway_task_id"):
                    task["gateway_task_id"] = event["gateway_task_id"]
                if event.get("gateway_state"):
                    task["gateway_state"] = event["gateway_state"]
                if event.get("message_summary"):
                    task["message_summary"] = event["message_summary"][:INBOUND_SUMMARY_CHARS]
                if event.get("reply_summary"):
                    task["reply_summary"] = event["reply_summary"][:INBOUND_SUMMARY_CHARS]
                if event.get("error_category"):
                    task["error_category"] = event["error_category"]
                task["last_observed_at"] = event.get("observed_at") or utc_timestamp()
                self.tasks[task_id] = task
                self._persist_locked()
            # 事件落日志（内存，供 conversation 摘要）
            self.append_event(task_id, {
                "type": "inbound",
                "role": "system",
                "ts": event.get("observed_at") or utc_timestamp(),
                "text": f"{event.get('phase')} | {new_state or 'state'}: {(event.get('message_summary') or event.get('reply_summary') or '')[:200]}",
            })
            return task_id, created

    def remove(self, task_id: str) -> dict[str, Any] | None:
        """从内存与持久化中移除任务、实时事件及结果文件（终态任务专用）。

        调用方负责删除关联的 Codex 会话文件。返回被移除的任务（未找到返回 None）。
        """
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                return None
            self.tasks.pop(task_id, None)
            self.events.pop(task_id, None)
            self.event_logs.pop(task_id, None)
            self._persist_locked()
        try:
            result_file = self.results_dir / f"{task_id}.txt"
            if result_file.exists():
                result_file.unlink()
        except OSError:
            logging.exception("Could not delete result file for %s", task_id)
        return task

    def cancel(self, task_id: str) -> dict[str, Any] | None:
        """Cancel a task atomically with its process termination."""
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            state = (task.get("status") or {}).get("state")
            if state in TERMINAL_STATES:
                return self._materialize_locked(task)
            context_id = task["contextId"]
            task["status"] = {
                "state": "TASK_STATE_CANCELED",
                "timestamp": utc_timestamp(),
                "message": agent_message("Codex 任务已取消。", context_id),
            }
            task["finished_at"] = utc_timestamp()
            self._persist_locked()
            self.events.setdefault(task_id, threading.Event()).set()
            # Kill the process tree while holding the store lock so a concurrent
            # completion update cannot race past the CANCELED state.
            process = self.processes.get(task_id)
            if process is not None:
                terminate_process_tree(process)
            return self._materialize_locked(task)


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        process.terminate()


class CodexRunResult:
    __slots__ = ("returncode", "session_id", "output", "stderr_tail", "stdout_tail", "timed_out")

    def __init__(
        self,
        returncode: int,
        session_id: str | None,
        output: str,
        stderr_tail: str,
        stdout_tail: str,
        timed_out: bool = False,
    ) -> None:
        self.returncode = returncode
        self.session_id = session_id
        self.output = output
        self.stderr_tail = stderr_tail
        self.stdout_tail = stdout_tail
        self.timed_out = timed_out


class CodexBridge:
    def __init__(
        self,
        *,
        codex: str | None,
        workspace: Path,
        state_dir: Path,
        model: str,
        sync_wait: int,
        codex_timeout: int,
        max_concurrent: int,
        max_tasks: int = 100,
        token: str | None = None,
        hide_orphan_tasks: bool = True,
        inbound_token: str | None = None,
    ) -> None:
        # ``codex`` is a hint (explicit path or None). It is re-resolved with
        # find_codex_executable before every spawn so a stale path self-heals.
        self.codex_hint = codex
        self.workspace = workspace.resolve()
        self.state_dir = state_dir.resolve()
        self.model = model
        self.sync_wait = sync_wait
        self.codex_timeout = codex_timeout
        self.max_concurrent = max_concurrent
        self.token = token
        self.hide_orphan_tasks = hide_orphan_tasks
        self.inbound_token = inbound_token
        self.started_at = time.time()
        self.semaphore = threading.BoundedSemaphore(max_concurrent)
        self.store = TaskStore(self.state_dir / "tasks.json", max_tasks=max_tasks)
        self.sessions = SessionStore(self.state_dir / "sessions.json")
        # 启动时清理卡死的 inbound 任务（MCP 崩溃遗留的 WORKING）
        try:
            affected = self.store.cleanup_stuck_inbound()
            if affected:
                logging.warning("Startup: marked %d stuck inbound tasks as FAILED", len(affected))
        except Exception:
            logging.exception("Startup inbound cleanup failed")

    def _resolve_codex(self) -> str:
        try:
            return find_codex_executable(self.codex_hint)
        except FileNotFoundError:
            time.sleep(1)
            return find_codex_executable(self.codex_hint)

    def _heartbeat_loop(self, task_id: str, stop_event: threading.Event) -> None:
        while not stop_event.wait(HEARTBEAT_INTERVAL_SECONDS):
            try:
                if not self.store.update_heartbeat(task_id):
                    return
            except Exception:
                logging.exception("Heartbeat update failed for %s", task_id)

    def _record_progress_event(self, task_id: str, item: dict[str, Any]) -> None:
        """Extract a human-readable progress event from a Codex ``item``.

        Codex's ``--json`` output emits ``item.completed`` entries with item
        types like ``agent_message`` (assistant text) and ``function_call`` /
        ``tool_call`` (tool invocations). We record a progress event; assistant
        text is kept up to 20000 chars (long final replies are not truncated),
        tool arguments are summarized.
        """
        try:
            item_type = item.get("type", "item")
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                summary = text.strip().replace("\r", " ").replace("\n", " ")
                if len(summary) > 20000:
                    summary = summary[:19997] + "..."
                self.store.append_event(task_id, {
                    "type": "message",
                    "role": "assistant",
                    "ts": utc_timestamp(),
                    "text": summary,
                })
                return
            # agent_reasoning（Codex 思考块）：存成 reasoning 事件（思考级流式）
            # 注意：部分模型 reasoning 字段叫 "reasoning"，部分叫 "summary"
            if item_type == "agent_reasoning":
                reason_text = (
                    item.get("reasoning")
                    or item.get("summary")
                    or item.get("text")
                    or ""
                )
                if isinstance(reason_text, str) and reason_text.strip():
                    r_summary = reason_text.strip().replace("\r", " ").replace("\n", " ")
                    if len(r_summary) > 500:
                        r_summary = r_summary[:497] + "..."
                    self.store.append_event(task_id, {
                        "type": "reasoning",
                        "role": "system",
                        "ts": utc_timestamp(),
                        "text": r_summary,
                    })
                    return
                # 无内容的 reasoning 块：记录存在但不带文本
                self.store.append_event(task_id, {
                    "type": "reasoning",
                    "role": "system",
                    "ts": utc_timestamp(),
                    "text": "[Codex 正在思考…]",
                })
                return
            # 不同 item 类型承载工具名的字段不同：
            #   function_call/tool_call -> name
            #   command_execution       -> command
            #   mcp_tool_call           -> server + tool
            name = (
                item.get("name")
                or item.get("tool_name")
                or item.get("command")
                or (
                    f"{item.get('server')}.{item.get('tool')}"
                    if item.get("server") and item.get("tool")
                    else None
                )
            )
            if item_type in ("function_call", "tool_call", "command_execution", "mcp_tool_call") and name:
                summary = f"调用工具 {name}"
                args = item.get("arguments") or item.get("input") or item.get("command_args") or ""
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                if isinstance(args, str) and args.strip():
                    args = args.strip().replace("\r", " ").replace("\n", " ")
                    if len(args) > 120:
                        args = args[:117] + "..."
                    summary += f" | {args}"
                self.store.append_event(task_id, {
                    "type": "tool",
                    "role": "tool",
                    "ts": utc_timestamp(),
                    "text": summary,
                })
                return
            self.store.append_event(task_id, {
                "type": "item",
                "role": "system",
                "ts": utc_timestamp(),
                "text": f"事件: {item_type}",
            })
        except Exception:
            logging.exception("Could not record progress event for %s", task_id)

    @staticmethod
    def _extract_summary(prompt: str, limit: int = 200) -> str:
        """从任务 prompt 提取一句话简介（格式无关）：
        1) 优先找【目标】段（四要素模板的概括）
        2) 没有则取第一句完整的话（句号/问号/感叹号/换行截断）
        3) 去掉常见引导词（请帮我/帮我/请 等）
        """
        text = prompt.strip()
        # 去掉桥注入的系统前缀（如果有）
        marker = "Hermes task:"
        idx = text.rfind(marker)
        if idx != -1:
            text = text[idx + len(marker):].strip()
        # 折叠空白
        text = " ".join(text.split())

        def _trim(s: str) -> str:
            s = s.strip()
            if len(s) <= limit:
                return s
            return s[: limit - 1] + "…"

        def _drop_lead(s: str) -> str:
            # 去掉长引导词（格式无关）：只剥"完成以下任务"类模板引导，
            # 保留"帮我"等自然口语开头（"帮我在网上找图"本身已是一句话）
            for pre in ("请帮我完成以下任务：", "请帮我完成：", "帮我完成以下任务：",
                        "帮我完成：", "请帮我完成", "请帮我：", "帮我：", "请帮我"):
                if s.startswith(pre):
                    s = s[len(pre):].strip()
                    break
            return s

        # 1) 优先【目标】段（四要素模板）
        g_idx = text.find("【目标】")
        if g_idx != -1:
            after = text[g_idx + len("【目标】"):].strip()
            nxt = after.find("【")
            if nxt != -1:
                after = after[:nxt].strip()
            after = _drop_lead(after)
            # 取第一句
            for sep in ("。", "！", "？", "；", "!", "?", ";"):
                p = after.find(sep)
                if p != -1:
                    after = after[:p + 1].strip()
                    break
            if after:
                return _trim(after)

        # 2) 回退：取第一句完整的话（格式无关）
        text = _drop_lead(text)
        for sep in ("。", "！", "？", "；", "!", "?", ";"):
            p = text.find(sep)
            if p != -1:
                text = text[:p + 1].strip()
                break
        # 换行截断（第一行）
        p = text.find("\n")
        if p != -1:
            text = text[:p].strip()
        if text:
            return _trim(text)

        # 3) 最后兜底
        return _trim(text) if text else ""

    @staticmethod
    def _bridge_prompt(prompt: str) -> str:
        return (
            "You are Codex receiving a task from Hermes over a local A2A bridge. "
            "Complete the task in the configured workspace, use tools as needed, and return a concise "
            "result that states what changed and what was verified. Do not expose chain-of-thought. "
            "Do not call call_hermes or any A2A/MCP tool that invokes Hermes or this bridge back; "
            "不得通过 call_hermes 或任何 A2A/MCP 工具反向调用 Hermes。\n\n"
            f"Hermes task:\n{prompt}"
        )

    def _new_session_command(self, codex: str, result_file: Path) -> list[str]:
        command = [
            codex,
            "exec",
            "--sandbox",
            "danger-full-access",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--json",
        ]
        if self.model:
            command += ["--model", self.model]
        command += ["--cd", str(self.workspace), "--output-last-message", str(result_file), "-"]
        return command

    def _resume_command(self, codex: str, session_id: str, result_file: Path) -> list[str]:
        command = [codex, "exec", "resume", "--skip-git-repo-check", "--json"]
        if self.model:
            command += ["--model", self.model]
        command += ["--output-last-message", str(result_file), session_id, "-"]
        return command

    def _exec_codex(
        self,
        command: list[str],
        prompt: str,
        task_id: str,
        context_id: str,
        heartbeat_stop: threading.Event,
    ) -> CodexRunResult:
        """Spawn Codex, stream stdout line by line, and persist the thread id
        as soon as ``thread.started`` is seen (instead of buffering all output)."""
        result_file = self.store.results_dir / f"{task_id}.txt"
        self.store.results_dir.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.workspace,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.store.attach_process(task_id, process)
        started_session_id: list[str | None] = [None]
        final_message: list[str] = [""]
        stdout_tail: deque[str] = deque(maxlen=STDOUT_TAIL_LINES)
        stderr_tail: list[str] = []
        stderr_chars = [0]
        state_lock = threading.Lock()

        def drain_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                stripped = line.strip()
                if not stripped:
                    continue
                with state_lock:
                    stdout_tail.append(line)
                try:
                    event = json.loads(stripped)
                except (json.JSONDecodeError, TypeError):
                    continue
                if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                    if started_session_id[0] is None:
                        started_session_id[0] = event["thread_id"]
                        try:
                            self.sessions.set(context_id, event["thread_id"])
                        except Exception:
                            logging.exception("Could not persist session id early for %s", context_id)
                    self.store.append_event(task_id, {
                        "type": "session",
                        "role": "system",
                        "ts": utc_timestamp(),
                        "text": f"Codex 会话已创建: {event['thread_id'][:12]}…",
                    })
                    continue
                item = event.get("item")
                if event.get("type") == "item.completed" and isinstance(item, dict):
                    self._record_progress_event(task_id, item)
                if (
                    event.get("type") == "item.completed"
                    and isinstance(item, dict)
                    and item.get("type") == "agent_message"
                    and isinstance(item.get("text"), str)
                ):
                    with state_lock:
                        final_message[0] = item["text"]

        def drain_stderr() -> None:
            assert process.stderr is not None
            for chunk in process.stderr:
                with state_lock:
                    if stderr_chars[0] < MAX_STDERR_CHARS:
                        stderr_tail.append(chunk)
                        stderr_chars[0] += len(chunk)

        stdout_thread = threading.Thread(
            target=drain_stdout, daemon=True, name=f"stdout-{task_id[-8:]}"
        )
        stderr_thread = threading.Thread(
            target=drain_stderr, daemon=True, name=f"stderr-{task_id[-8:]}"
        )
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        try:
            assert process.stdin is not None
            try:
                process.stdin.write(prompt)
                process.stdin.write("\n")
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                process.wait(timeout=self.codex_timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_process_tree(process)
                process.wait()
        finally:
            stdout_thread.join(timeout=10)
            stderr_thread.join(timeout=10)
            self.store.detach_process(task_id)

        stdout_text = "".join(stdout_tail)
        stderr_text = "".join(stderr_tail)[-MAX_STDERR_CHARS:]
        output = ""
        try:
            if result_file.exists():
                output = result_file.read_text(encoding="utf-8").strip()
        except OSError:
            logging.exception("Could not read result file %s", result_file)
        if not output:
            output = (final_message[0] or stdout_text).strip()
        return CodexRunResult(
            returncode=process.returncode,
            session_id=started_session_id[0],
            output=output,
            stderr_tail=stderr_text,
            stdout_tail=stdout_text,
            timed_out=timed_out,
        )

    def card(self, base_url: str) -> dict[str, Any]:
        return {
            "name": "Codex CLI",
            "description": "Local coding agent exposed to Hermes through an A2A-to-Codex bridge.",
            "version": "1.0.0",
            "protocolVersion": "1.0",
            "url": base_url,
            "provider": {"organization": "Local Codex", "url": base_url},
            "supportedInterfaces": [
                {"url": base_url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
            ],
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
                "stateTransitionHistory": False,
                "extendedAgentCard": False,
            },
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [
                {
                    "id": "coding",
                    "name": "coding",
                    "description": "Plan, implement, debug, review, test, and verify coding tasks.",
                    "tags": ["coding", "debugging", "review", "testing"],
                }
            ],
        }

    def start_task(self, prompt: str, context_id: str) -> str:
        # 工作线解析：调用方传逻辑名（无 ctx- 前缀）时，由桥解析当前代次
        if context_id and not context_id.startswith("ctx-"):
            workstream_name = context_id
            context_id = self.sessions.resolve_workstream(
                workstream_name,
                profile=str(getattr(self, "profile_name", "") or ""),
                workspace=str(self.workspace),
                model=self.model,
            )
            self._current_workstream = workstream_name
        else:
            self._current_workstream = None
        task_id = f"task-{uuid.uuid4().hex}"
        with self.store.lock:
            if self.store.active_count() >= self.max_concurrent:
                rejected = {
                    "id": task_id,
                    "contextId": context_id,
                    "status": {
                        "state": "TASK_STATE_REJECTED",
                        "timestamp": utc_timestamp(),
                        "message": agent_message(
                            f"Codex 任务队列已满（同时最多 {self.max_concurrent} 个任务），已拒绝本次请求。"
                            "请等待现有任务完成或结束后重试。",
                            context_id,
                        ),
                    },
                    "created_at": utc_timestamp(),
                    "finished_at": utc_timestamp(),
                }
                self.store.add(rejected)
                return task_id
            task = {
                "id": task_id,
                "contextId": context_id,
                "status": {
                    "state": "TASK_STATE_WORKING",
                    "timestamp": utc_timestamp(),
                    "message": agent_message("Codex 正在执行任务。", context_id),
                },
                "created_at": utc_timestamp(),
                "summary": self._extract_summary(prompt),
            }
            session_id = self.sessions.get(context_id)
            if session_id:
                task["session_id"] = session_id
            self.store.add(task)
            # 用户消息记为对话流第一条（role=user）
            self.store.append_event(task_id, {
                "type": "message",
                "role": "user",
                "ts": utc_timestamp(),
                "text": prompt.strip()[:2000],
            })
        thread = threading.Thread(
            target=self._run_task,
            args=(task_id, prompt),
            name=f"codex-{task_id[-8:]}",
            daemon=True,
        )
        thread.start()
        return task_id

    def _run_task(self, task_id: str, prompt: str) -> None:
        with self.semaphore:
            current = self.store.get(task_id)
            if not current or current["status"]["state"] == "TASK_STATE_CANCELED":
                return
            context_id = current["contextId"]
            session_id = self.sessions.get(context_id)
            resumed_from_new = False
            heartbeat_stop = threading.Event()
            heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                args=(task_id, heartbeat_stop),
                name=f"hb-{task_id[-8:]}",
                daemon=True,
            )
            heartbeat_thread.start()
            try:
                codex = self._resolve_codex()
                result: CodexRunResult | None = None
                attempts = 0
                while True:
                    attempts += 1
                    if self.store.is_canceled(task_id):
                        return
                    use_resume = bool(session_id)
                    if use_resume:
                        command = self._resume_command(
                            codex, session_id, self.store.results_dir / f"{task_id}.txt"
                        )
                        logging.info("Resuming Codex session %s for %s", session_id, task_id)
                    else:
                        command = self._new_session_command(
                            codex, self.store.results_dir / f"{task_id}.txt"
                        )
                        logging.info("Starting new Codex session for %s in %s", task_id, self.workspace)
                    result = self._exec_codex(
                        command,
                        self._bridge_prompt(prompt),
                        task_id,
                        context_id,
                        heartbeat_stop,
                    )
                    if self.store.is_canceled(task_id):
                        return
                    if result.returncode == 0:
                        # codex exec resume can exit 0 while silently starting a
                        # different thread (e.g. the requested session no longer
                        # exists). Treat that as a failed resume and fall back.
                        if (
                            use_resume
                            and result.session_id
                            and result.session_id != session_id
                        ):
                            logging.warning(
                                "Resume did not continue session %s (new thread %s); "
                                "retrying with a fresh session",
                                session_id,
                                result.session_id,
                            )
                            session_id = None
                            resumed_from_new = True
                            continue
                        break
                    # Self-heal: a failed resume falls back to a fresh session once.
                    if use_resume and attempts < 2:
                        logging.warning(
                            "Resume failed for %s (rc=%s); retrying with a fresh session",
                            session_id,
                            result.returncode,
                        )
                        session_id = None
                        resumed_from_new = True
                        continue
                    detail = result.stderr_tail or result.stdout_tail or f"exit code {result.returncode}"
                    raise RuntimeError(detail[-12000:])

                if result is None:
                    raise RuntimeError("Codex did not run")
                if result.session_id:
                    self.sessions.set(context_id, result.session_id)
                    if not session_id:
                        session_id = result.session_id
                output = result.output or "Codex 已完成任务，但没有返回文本结果。"
                output = output[:MAX_RESULT_CHARS]
                completed = {
                    "id": task_id,
                    "contextId": context_id,
                    "session_id": session_id,
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "timestamp": utc_timestamp(),
                        "message": agent_message(output, context_id),
                    },
                    "artifacts": [
                        {
                            "artifactId": f"artifact-{uuid.uuid4().hex}",
                            "name": "Codex result",
                            "parts": [text_part(output)],
                        }
                    ],
                }
                if resumed_from_new:
                    completed["resumed_from_new"] = True
                if not self.store.update(
                    task_id, completed, terminal=True, if_not_state="TASK_STATE_CANCELED"
                ):
                    logging.info("Canceled after completion %s", task_id)
                    return
                # 工作线元数据更新（消息数/token 估算/会话 id）
                if self._current_workstream and session_id:
                    stats = estimate_session_stats(session_id)
                    self.sessions.touch_workstream(
                        self._current_workstream,
                        session_id=session_id,
                        message_count=stats["message_count"],
                        estimated_tokens=stats["estimated_tokens"],
                        file_size=stats["file_size"],
                    )
                logging.info("Completed %s", task_id)
            except Exception as error:
                logging.exception("Failed %s", task_id)
                failed = {
                    "id": task_id,
                    "contextId": context_id,
                    "status": {
                        "state": "TASK_STATE_FAILED",
                        "timestamp": utc_timestamp(),
                        "message": agent_message(
                            f"Codex 执行失败：{sanitize_error(str(error))}", context_id
                        ),
                    },
                }
                if session_id:
                    failed["session_id"] = session_id
                if resumed_from_new:
                    failed["resumed_from_new"] = True
                self.store.update(
                    task_id, failed, terminal=True, if_not_state="TASK_STATE_CANCELED"
                )
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=3)

    def metrics(self) -> dict[str, Any]:
        counts = {
            "total": 0,
            "active": 0,
            "completed": 0,
            "failed": 0,
            "canceled": 0,
            "rejected": 0,
        }
        durations: list[float] = []
        with self.store.lock:
            for task in self.store.tasks.values():
                state = (task.get("status") or {}).get("state")
                counts["total"] += 1
                if state == "TASK_STATE_WORKING":
                    counts["active"] += 1
                elif state == "TASK_STATE_COMPLETED":
                    counts["completed"] += 1
                elif state == "TASK_STATE_FAILED":
                    counts["failed"] += 1
                elif state == "TASK_STATE_CANCELED":
                    counts["canceled"] += 1
                elif state == "TASK_STATE_REJECTED":
                    counts["rejected"] += 1
                start = task.get("created_at") or task.get("started_at")
                end = task.get("finished_at")
                if start and end:
                    try:
                        durations.append(_iso_to_epoch(end) - _iso_to_epoch(start))
                    except ValueError:
                        continue
        avg_duration = sum(durations) / len(durations) if durations else 0.0
        return {
            "service": "codex-a2a-bridge",
            "tasks": counts,
            "avgDurationSeconds": round(avg_duration, 3),
            "uptimeSeconds": round(time.time() - self.started_at, 2),
            "timestamp": utc_timestamp(),
        }

    def delete_task(self, task_id: str) -> tuple[bool, str, dict | None]:
        """删除一个终态任务：桥记录 + 实时事件 + 结果文件 + 关联的 Codex 会话文件。

        返回 (成功?, 消息, 被删任务或 None)。规则：
        - WORKING/不存在 -> 拒绝
        - 该 context_id 下还有其他任务 -> 只删桥记录，保留 Codex 会话文件
          （兄弟任务还依赖这个会话延续上下文）
        - 无兄弟任务 -> 连同 Codex 会话文件一起删

        整个流程在锁内完成（检查兄弟、删记录、定位并删除会话文件），
        避免 TOCTOU：锁外新加入的同一 context 任务不会被误删会话。
        """
        with self.store.lock:
            task = self.store.get(task_id)
            if task is None:
                return False, f"task not found: {task_id}", None
            state = (task.get("status") or {}).get("state")
            if state == "TASK_STATE_WORKING":
                return False, "cannot delete a WORKING task", task
            context_id = task.get("contextId") or ""
            # 检查 context_id 下是否还有其他任务
            siblings = [
                t["id"] for t in self.store.tasks.values()
                if t.get("contextId") == context_id and t["id"] != task_id
            ]
            session_id = self.sessions.get(context_id) if context_id else None
            removed = self.store.remove(task_id)
            if removed is None:
                return False, f"task not found: {task_id}", None

            deleted_session_file = False
            is_inbound = (removed.get("direction") == "inbound")
            if is_inbound:
                # inbound 无 Codex 会话文件；记录 tombstone 防 outbox 重投复活
                src = removed.get("source") or "hermes_mcp"
                op = removed.get("operation_id") or ""
                if op:
                    self.store.deleted_inbound_ops.add(f"{src}|{op}")
                return True, "deleted", removed
            if session_id and not siblings:
                # 无兄弟任务：删 Codex 会话文件（尽力而为，失败不阻塞删除）
                try:
                    path = find_codex_session_file(session_id)
                    if path is not None:
                        path.unlink(missing_ok=True)
                        deleted_session_file = True
                    self.sessions.remove(context_id)
                except OSError:
                    logging.exception("Could not delete Codex session file for %s", session_id)
        return True, "deleted", removed

    def handle_inbound_event(self, event: dict[str, Any]) -> tuple[str, bool]:
        """处理 MCP 上报的 inbound 事件，返回 (task_id, created?)。"""
        return self.store.upsert_inbound(event)

    def query_tasks(self, states: list[str] | None = None) -> list[dict[str, Any]]:
        return self.store.query(states)

    def shutdown(self) -> None:
        with self.store.lock:
            processes = list(self.store.processes.values())
            self.store.processes.clear()
        for process in processes:
            try:
                terminate_process_tree(process)
            except Exception:
                logging.exception("Failed to terminate process during bridge shutdown")
        logging.info("Bridge shutdown: terminated %d tracked Codex processes", len(processes))


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], bridge: CodexBridge) -> None:
        super().__init__(address, A2AHandler)
        self.bridge = bridge


class A2AHandler(BaseHTTPRequestHandler):
    server: BridgeServer

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.info("HTTP %s - %s", self.address_string(), fmt % args)

    def _authorized(self) -> bool:
        token = getattr(self.server.bridge, "token", None)
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        supplied = header[len("Bearer ") :].strip()
        return secrets.compare_digest(supplied, token)

    def _base_url(self) -> str:
        host = self.headers.get("Host") or ""
        if not _ALLOWED_CARD_HOST_RE.match(host.strip()):
            host = f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        return f"http://{host}"

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _rpc_error(self, req_id: Any, code: int, message: str, status: int = 400) -> None:
        self._send_json(
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
            status,
        )

    def _send_unauthorized(self) -> None:
        self._send_json(
            {"error": "unauthorized", "message": "missing or invalid Bearer token"},
            401,
        )

    # Monitoring endpoints are read-only, loopback-only views over local task
    # state. They are intentionally exempt from Bearer auth because the HTML
    # page and its JS polling cannot attach an Authorization header; they
    # expose no secrets beyond what the local operator already has.
    _UNPROTECTED_GET = {"/ui", "/health", "/tasks", "/monitor-token"}

    def _serve_monitor_ui(self, token: str = "") -> None:
        html = MONITOR_UI_HTML
        if token:
            # 注入到 <script> 开头的 token 变量（JSON 转义防注入）
            import json as _json
            html = html.replace(
                "const TOKEN = null;",
                "const TOKEN = " + _json.dumps(token) + ";",
                1,
            )
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_task_list(self) -> None:
        # 查询前顺手清理卡死 inbound（防堆积，低成本）
        try:
            self.server.bridge.store.cleanup_stuck_inbound()
        except Exception:
            pass
        tasks = self.server.bridge.store.query_brief()
        if self.server.bridge.hide_orphan_tasks:
            # 只显示"还有对话可看"的任务：WORKING 进行中始终显示；
            # 终态任务要求 Codex 会话文件仍存在（被清理的自动消失）
            index = session_index()
            visible = []
            for t in tasks:
                state = t.get("state")
                if state == "WORKING":
                    visible.append(t)
                    continue
                # inbound（反向链路）没有 Codex 会话文件，只要事件有效就显示
                if t.get("direction") == "inbound":
                    visible.append(t)
                    continue
                context_id = t.get("contextId") or ""
                session_id = self.server.bridge.sessions.get(context_id) if context_id else None
                if session_id and session_id in index:
                    visible.append(t)
            tasks = visible
        self._send_json({"tasks": tasks})

    def _serve_task_conversation(self, task_id: str) -> None:
        """返回任务的完整对话：优先内存事件流，历史任务回退到 Codex 会话文件解析。

        返回 {taskId, messages: [{role, ts, text}], source: memory|file|none}
        """
        task = self.server.bridge.store.get(task_id)
        if task is None:
            self._send_json({"taskId": task_id, "messages": [], "source": "none"})
            return
        # inbound（反向链路）：无 Codex 会话文件，直接返回上报摘要
        if task.get("direction") == "inbound":
            messages = []
            if task.get("message_summary"):
                messages.append({"role": "user", "ts": task.get("created_at", ""), "text": task.get("message_summary")})
            if task.get("reply_summary"):
                messages.append({"role": "assistant", "ts": task.get("last_observed_at", ""), "text": task.get("reply_summary")})
            if not messages:
                messages.append({"role": "system", "ts": task.get("created_at", ""), "text": "反向调用（Codex→Hermes），暂无内容摘要"})
            self._send_json({"taskId": task_id, "messages": messages, "source": "inbound-report"})
            return
        # 内存事件流（进行中任务 / 本桥生命周期内）
        events, _ = self.server.bridge.store.events_since(task_id, after=-1)
        if events:
            messages = [
                {"role": e.get("role", "assistant"), "ts": e.get("ts", ""), "text": e.get("text", "")}
                for e in events
            ]
            self._send_json({"taskId": task_id, "messages": messages, "source": "memory"})
            return
        # 历史任务：从 Codex 会话文件解析
        context_id = task.get("contextId") or ""
        session_id = self.server.bridge.sessions.get(context_id)
        if session_id:
            messages = parse_codex_conversation(session_id)
            if messages:
                self._send_json({"taskId": task_id, "messages": messages, "source": "file"})
                return
        self._send_json({"taskId": task_id, "messages": [], "source": "none"})

    def _serve_task_events(self, task_id: str) -> None:
        after_raw = (self.path.split("?", 1)[1] if "?" in self.path else "")
        after = -1
        for pair in after_raw.split("&"):
            if pair.startswith("after="):
                try:
                    after = int(pair[len("after="):])
                except ValueError:
                    pass
        events, latest = self.server.bridge.store.events_since(task_id, after=after)
        self._send_json({"taskId": task_id, "events": events, "latest": latest})

    @classmethod
    def _is_monitor_get(cls, path: str) -> bool:
        """监控端点：/ui、/tasks、/tasks/<id>/events、/tasks/<id>/conversation（loopback 只读）。"""
        if path in cls._UNPROTECTED_GET:
            return True
        return (path.startswith("/tasks/") and path.endswith("/events")) or (
            path.startswith("/tasks/") and path.endswith("/conversation")
        )

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if self._is_monitor_get(path):
            # 监控端点只服务回环 Host（防 DNS-rebinding / 浏览器侧滥用）
            host = (self.headers.get("Host") or "").strip()
            if not _ALLOWED_CARD_HOST_RE.match(host):
                self._send_json({"error": "bad host"}, 400)
                return
        elif not self._authorized():
            self._send_unauthorized()
            return
        if path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            self._send_json(self.server.bridge.card(self._base_url()))
            return
        if path == "/health":
            self._send_json({"ok": True, "service": "codex-a2a-bridge"})
            return
        if path == "/metrics":
            self._send_json(self.server.bridge.metrics())
            return
        if path == "/monitor-token":
            # 监控页自动获取 token（仅限回环 Host，防 DNS-rebinding）。
            # 返回桥的主 token，供页面 DELETE 鉴权。
            host = (self.headers.get("Host") or "").strip()
            if not _ALLOWED_CARD_HOST_RE.match(host):
                self._send_json({"error": "bad host"}, 400)
                return
            tok = getattr(self.server.bridge, "token", None) or ""
            self._send_json({"token": tok})
            return
        if path == "/ui":
            # 支持 ?token= 注入：open_preview 打开时带上 token，
            # 页面 JS 存入 sessionStorage 供 DELETE 鉴权使用
            token = ""
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            for pair in query.split("&"):
                if pair.startswith("token="):
                    token = pair[len("token="):]
                    break
            self._serve_monitor_ui(token)
            return
        if path.startswith("/internal/tasks/"):
            # 内部窄接口：单任务查询（SDK sidecar 专用，Bearer 已校验）
            task_id = path[len("/internal/tasks/"):].split("/", 1)[0]
            task = self.server.bridge.store.get(task_id)
            if task is None:
                self._send_json({"error": "task not found"}, 404)
                return
            # 精简字段（避免泄露内部细节）
            status = task.get("status") or {}
            self._send_json({
                "id": task.get("id"),
                "state": status.get("state"),
                "direction": task.get("direction"),
                "contextId": task.get("contextId"),
                "created_at": task.get("created_at"),
                "finished_at": task.get("finished_at"),
                "summary": task.get("summary", ""),
            })
            return

        if path == "/tasks":
            self._serve_task_list()
            return
        if path == "/workstreams":
            self._send_json({"workstreams": self.server.bridge.sessions.list_workstreams()})
            return
        if path.startswith("/tasks/") and path.endswith("/conversation"):
            self._serve_task_conversation(path[len("/tasks/"):-len("/conversation")])
            return
        if path.startswith("/tasks/") and path.endswith("/events"):
            self._serve_task_events(path[len("/tasks/"):-len("/events")])
            return
        self._send_json({"error": "not found"}, 404)

    def do_DELETE(self) -> None:
        """DELETE /tasks/<id> — 删除终态任务（需 Bearer token）。"""
        if not self._authorized():
            self._send_unauthorized()
            return
        path = self.path.split("?", 1)[0]
        if not path.startswith("/tasks/"):
            self._send_json({"error": "not found"}, 404)
            return
        task_id = path[len("/tasks/"):]
        if not task_id:
            self._send_json({"error": "missing task id"}, 400)
            return
        ok, message, _ = self.server.bridge.delete_task(task_id)
        if not ok:
            self._send_json({"error": message}, 404 if "not found" in message else 409)
            return
        self._send_json({"ok": True, "message": message, "deleted": task_id})

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            self._rpc_error(None, -32600, "invalid request body size", 413)
            return
        try:
            request = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._rpc_error(None, -32700, "parse error")
            return
        if not isinstance(request, dict):
            self._rpc_error(None, -32600, "request must be an object")
            return

        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if method == "inbound/events":
            # 反向链路上报（Codex->Hermes 的 MCP server 调这个端点）
            # 独立 token 校验在 _handle_inbound_event 内完成（inbound_token）
            if not isinstance(params, dict):
                self._rpc_error(req_id, -32602, "params must be an object")
                return
            self._handle_inbound_event(request, params)
            return

        if not self._authorized():
            self._send_unauthorized()
            return
        if not isinstance(params, dict):
            self._rpc_error(req_id, -32602, "params must be an object")
            return

        # ---- internal/* 内部窄接口（SDK sidecar 专用，Bearer 已校验）----
        if method == "internal/submit":
            prompt = str(params.get("prompt", ""))
            context_id = str(params.get("context_id", "") or "")
            if not prompt.strip():
                self._rpc_error(req_id, -32602, "prompt is required")
                return
            try:
                task_id = self.server.bridge.start_task(prompt, context_id)
                self._send_json({"ok": True, "task_id": task_id})
            except Exception as e:  # pragma: no cover
                logging.exception("internal/submit failed")
                self._rpc_error(req_id, -32603, f"submit failed: {e}")
            return

        if method == "internal/cancel":
            task_id = str(params.get("task_id", ""))
            if not task_id:
                self._rpc_error(req_id, -32602, "task_id is required")
                return
            ok, message, _ = self.server.bridge.delete_task(task_id)
            self._send_json({"ok": ok, "message": message})
            return

        if method in ("SendMessage", "message/send"):
            try:
                ensure_text_only_message(params)
            except ContentTypeNotSupportedError as exc:
                self._rpc_error(req_id, CONTENT_TYPE_NOT_SUPPORTED, sanitize_error(str(exc)))
                return
            prompt = extract_message_text(params)
            if not prompt:
                self._rpc_error(req_id, -32602, "message must contain a non-empty text part")
                return
            immediate = wants_immediate_return(params)
            context_id = resolve_context_id(params)
            task_id = self.server.bridge.start_task(prompt, context_id)
            if not immediate:
                self.server.bridge.store.wait(task_id, self.server.bridge.sync_wait)
            task = self.server.bridge.store.get(task_id)
            if not task:
                self._rpc_error(req_id, -32603, "task disappeared")
                return
            if task["status"]["state"] == "TASK_STATE_WORKING":
                task["status"]["message"] = agent_message(
                    f"Codex 仍在执行。请稍后使用 GetTask 查询任务 {task_id}。", context_id
                )
            # Hermes accepts the bare Task and explicitly relies on this shape.
            self._send_json({"jsonrpc": "2.0", "id": req_id, "result": task})
            return

        if method in ("GetTask", "tasks/get"):
            task_id = str(params.get("id") or params.get("taskId") or params.get("task_id") or "")
            task = self.server.bridge.store.get(task_id)
            if not task:
                self._rpc_error(req_id, -32001, f"task not found: {task_id}", 404)
                return
            self._send_json({"jsonrpc": "2.0", "id": req_id, "result": task})
            return

        if method in ("CancelTask", "tasks/cancel"):
            task_id = str(params.get("id") or params.get("taskId") or params.get("task_id") or "")
            task = self.server.bridge.store.cancel(task_id)
            if not task:
                self._rpc_error(req_id, -32001, f"task not found: {task_id}", 404)
                return
            self._send_json({"jsonrpc": "2.0", "id": req_id, "result": task})
            return

        if method in ("QueryTasks", "tasks/query"):
            state_filter = params.get("state")
            states = params.get("states")
            if isinstance(state_filter, str):
                states = [state_filter]
            if states is None:
                tasks = self.server.bridge.query_tasks(None)
            elif isinstance(states, list):
                tasks = self.server.bridge.query_tasks(states)
            else:
                self._rpc_error(req_id, -32602, "states must be a list")
                return
            self._send_json({"jsonrpc": "2.0", "id": req_id, "result": {"tasks": tasks}})
            return

        self._rpc_error(req_id, -32601, f"method not found: {method}")

    def _handle_inbound_event(self, request: dict, params: dict) -> None:
        """处理 MCP 上报的 inbound 事件（反向链路 Codex->Hermes 监控）。

        独立 token 校验（inbound_token）；schema 校验；按 operation_id 幂等合并。
        返回 202 已接收 / 200 重复事件 / 400 非法 / 401 未授权。
        """
        req_id = request.get("id")
        bridge = self.server.bridge
        # 独立 token 校验：未配置 inbound_token 时端点不开放（防匿名写入）
        if not bridge.inbound_token:
            self._send_json({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": "inbound reporting disabled (no token configured)"}}, 403)
            return
        auth = self.headers.get("Authorization", "")
        if not secrets.compare_digest(auth, f"Bearer {bridge.inbound_token}"):
            self._send_json({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": "unauthorized"}}, 401)
            return
        if not isinstance(params, dict):
            self._rpc_error(req_id, -32602, "params must be an object")
            return
        event_id = params.get("event_id")
        operation_id = params.get("operation_id")
        if not isinstance(event_id, str) or not event_id or not isinstance(operation_id, str) or not operation_id:
            self._rpc_error(req_id, -32602, "event_id and operation_id (non-empty strings) required")
            return
        phase = params.get("phase")
        allowed_phases = {"started", "accepted", "state", "finished", "reconcile"}
        if phase not in allowed_phases:
            self._rpc_error(req_id, -32602, f"phase must be one of {sorted(allowed_phases)}")
            return
        # source 类型校验（防不可哈希毒化 dedup_key）
        source = params.get("source")
        if source is not None and not isinstance(source, str):
            self._rpc_error(req_id, -32602, "source must be a string")
            return
        dedup_key = (source or "hermes_mcp", operation_id, event_id)
        seen = bridge.store.seen_inbound_events
        with bridge.store.lock:
            if dedup_key in seen:
                self._send_json({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True, "duplicate": True}})
                return
        # 先落库成功，再记 seen（非法事件不毒化 event_id，可重试）
        try:
            task_id, created = bridge.handle_inbound_event(params)
        except (ValueError, TypeError) as exc:
            self._rpc_error(req_id, -32602, f"invalid event: {exc}")
            return
        with bridge.store.lock:
            seen.add(dedup_key)
            if len(seen) > 1000:
                for _ in range(len(seen) - 500):
                    seen.pop(next(iter(seen)))
        self._send_json({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True, "task_id": task_id, "created": created}}, 202)


def configure_logging(log_file: Path, verbose: bool) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    ]
    if verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def resolve_token(explicit: str | None, token_file: Path | None, state_dir: Path) -> str | None:
    value = explicit or os.environ.get("A2A_BRIDGE_TOKEN")
    if value and value.strip():
        return value.strip()
    path = token_file or (state_dir / "bridge.token")
    try:
        if path.is_file():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return content
    except OSError:
        logging.warning("Could not read token file %s", path)
    return None


def cleanup_residual_state(state_dir: Path) -> None:
    try:
        for leftover in state_dir.glob("codex-result-*.txt"):
            leftover.unlink(missing_ok=True)
            logging.info("Removed residual file %s", leftover.name)
    except OSError:
        logging.exception("Could not clean residual codex-result files in %s", state_dir)


def tighten_state_dir_permissions(state_dir: Path) -> None:
    """Best-effort ACL hardening on Windows; never fatal."""
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", str(state_dir), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        logging.info("Tightened ACL on %s", state_dir)
    except Exception as exc:
        logging.warning("Could not tighten ACL on %s (best-effort): %s", state_dir, exc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A2A bridge from Hermes to Codex CLI")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--state-dir", type=Path, default=Path(__file__).resolve().parent / ".codex-a2a")
    parser.add_argument("--codex", help="Path to codex.exe or codex.cmd")
    parser.add_argument("--max-tasks", type=int, default=100,
                        help="tasks.json 上限（默认 100，超出自动淘汰最旧任务）")
    parser.add_argument("--model", default="")
    parser.add_argument("--sync-wait", type=int, default=DEFAULT_SYNC_WAIT)
    parser.add_argument("--codex-timeout", type=int, default=DEFAULT_CODEX_TIMEOUT)
    parser.add_argument("--max-concurrent", type=int, default=1)
    parser.add_argument(
        "--token",
        help="Shared Bearer token (overrides A2A_BRIDGE_TOKEN and the token file)",
    )
    parser.add_argument(
        "--inbound-token",
        default=os.environ.get("INBOUND_BRIDGE_TOKEN", ""),
        help="Token for POST /inbound/events (reverse-link Codex->Hermes reports)",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        help="File containing the Bearer token (default: <state-dir>/bridge.token)",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Refusing non-loopback bind: danger-full-access Codex must remain local")
    if not args.workspace.is_dir():
        raise SystemExit(f"Workspace does not exist: {args.workspace}")
    if args.sync_wait < 1 or args.codex_timeout < args.sync_wait:
        raise SystemExit("Require 1 <= sync-wait <= codex-timeout")
    if args.max_concurrent < 1:
        raise SystemExit("max-concurrent must be positive")

    state_dir = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    tighten_state_dir_permissions(state_dir)
    configure_logging(state_dir / "bridge.log", args.verbose)

    token = resolve_token(args.token, args.token_file, state_dir)
    if token:
        logging.info("Auth enabled via shared Bearer token (A2A_BRIDGE_TOKEN/--token/token file)")

    # Fail fast at startup, then re-resolve before every spawn (with one retry).
    codex_hint = args.codex or os.environ.get("CODEX_CLI_PATH")
    find_codex_executable(codex_hint)

    cleanup_residual_state(state_dir)
    if args.max_tasks < 1:
        parser.error("--max-tasks must be >= 1")

    bridge = CodexBridge(
        codex=codex_hint,
        workspace=args.workspace,
        state_dir=state_dir,
        model=args.model,
        sync_wait=args.sync_wait,
        codex_timeout=args.codex_timeout,
        max_concurrent=args.max_concurrent,
        max_tasks=args.max_tasks,
        token=token,
        inbound_token=args.inbound_token,
    )
    server = BridgeServer((args.host, args.port), bridge)
    logging.info(
        "Codex A2A bridge listening at http://%s:%s (workspace=%s)",
        args.host,
        args.port,
        bridge.workspace,
    )

    def stop_server(_signum: int, _frame: Any) -> None:
        bridge.shutdown()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, stop_server)
        except (ValueError, OSError):
            logging.warning("Could not register handler for signal %s", sig)

    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        bridge.shutdown()
        server.server_close()
        logging.info("Codex A2A bridge stopped")


if __name__ == "__main__":
    main()
