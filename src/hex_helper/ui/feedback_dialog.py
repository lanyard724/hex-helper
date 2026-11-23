"""LoL V16.23 Hextech Problem Feedback & Rolling 20-Match Log Upload Dialog.

Strictly adheres to the desktop Hextech aesthetic (obsidian #040E1A/#091428, antique gold
#C8AA6E/#F0E6D2, Hextech cyan #0AC8B9) — zero Web Admin Tactile Orange tokens.
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import platform
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from hex_helper.edition import APP_VERSION
from hex_helper.logging_ring import MatchLogRing, compress_log_bundle
from hex_helper.ui.hextech_widgets import HextechActionButton
from hex_helper.ui.theme import body_font, display_font, latin_serif_font, load_app_icon

FEEDBACK_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("ocr_picker", "选卡识别异常"),
    ("build_ai", "出装 / AI 参谋"),
    ("overlay_ui", "浮窗 / 快捷键 / 卡顿"),
    ("account_billing", "账号 / 充值 / 更新"),
    ("other", "功能建议 / 其他"),
)


class _SubmitBridge(QObject):
    succeeded = Signal(dict)
    failed = Signal(str)


class FeedbackDialog(QDialog):
    """One-click player feedback dialog with 20-match rolling sanitized log bundling."""

    def __init__(
        self,
        *,
        log_ring: MatchLogRing,
        submit_fn: Callable[[dict[str, Any]], dict[str, Any]],
        diag_snapshot: dict[str, Any] | None = None,
        dataset_patch: str = "",
        dataset_mode: str = "",
        username: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._log_ring = log_ring
        self._submit_fn = submit_fn
        self._diag_snapshot = dict(diag_snapshot or {})
        self._dataset_patch = (dataset_patch or "").strip()
        self._dataset_mode = (dataset_mode or "").strip()
        self._username = (username or "").strip()
        self._selected_category = "ocr_picker"
        self._cat_buttons: dict[str, QPushButton] = {}
        self._submitting = False
        self._last_ticket_no = ""
        self._cached_bundle_text: str | None = None
        self._cached_bundle_matches: int = 0
        self._cached_bundle_gz_b64: str = ""

        self._bridge = _SubmitBridge(self)
        self._bridge.succeeded.connect(self._on_submit_succeeded)
        self._bridge.failed.connect(self._on_submit_failed)

        self.setWindowTitle("HexHelper · 问题反馈与日志上报")
        self.setWindowIcon(load_app_icon())
        self.setModal(True)
        self.resize(620, 620)
        self.setMinimumSize(540, 520)

        self._apply_hextech_style()
        self._build_ui()
        self._refresh_log_summary()

    def _apply_hextech_style(self) -> None:
        self.setStyleSheet(
            """
            QDialog {
                background-color: #040E1A;
                color: #F0E6D2;
            }
            QFrame#OuterFrame {
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #061425, stop:0.55 #09192D, stop:1 #030A14);
                border: 1px solid #785A28;
            }
            QFrame#HeaderBar {
                background-color: rgba(2, 10, 20, 235);
                border-bottom: 1px solid rgba(200, 170, 110, 140);
            }
            QFrame#LogCard {
                background-color: rgba(5, 16, 30, 225);
                border: 1px solid rgba(10, 200, 185, 110);
                border-left: 3px solid #0AC8B9;
                border-radius: 2px;
            }
            QFrame#SuccessCard {
                background-color: rgba(6, 34, 40, 220);
                border: 1px solid rgba(10, 200, 185, 190);
                border-left: 3px solid #0AC8B9;
                border-radius: 2px;
            }
            QLabel#SectionLbl {
                color: #C8AA6E;
                font-size: 11px;
                font-weight: bold;
                background: transparent;
                border: none;
            }
            QPushButton#CatPill {
                background-color: rgba(7, 20, 36, 220);
                color: #A09B8C;
                border: 1px solid rgba(120, 90, 40, 145);
                border-radius: 2px;
                padding: 5px 10px;
                font-size: 11px;
                font-weight: bold;
            }
            QPushButton#CatPill:hover {
                color: #F0E6D2;
                border-color: #C8AA6E;
                background-color: rgba(14, 34, 56, 235);
            }
            QPushButton#CatPill[active="true"] {
                color: #040E1A;
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #0AC8B9, stop:1 #0397AB);
                border: 1px solid #A3F5EE;
            }
            QPushButton#MiniLinkBtn {
                background-color: rgba(10, 24, 42, 220);
                color: #C8AA6E;
                border: 1px solid rgba(120, 90, 40, 160);
                border-radius: 2px;
                padding: 3px 9px;
                font-size: 11px;
            }
            QPushButton#MiniLinkBtn:hover {
                color: #F0E6D2;
                border-color: #0AC8B9;
                background-color: rgba(12, 34, 54, 240);
            }
            QTextEdit, QLineEdit, QPlainTextEdit {
                background-color: rgba(2, 10, 20, 240);
                color: #F0E6D2;
                border: 1px solid rgba(120, 90, 40, 165);
                border-radius: 2px;
                padding: 6px 8px;
                selection-background-color: #0397AB;
                selection-color: #FFFFFF;
            }
            QTextEdit:focus, QLineEdit:focus {
                border-color: #0AC8B9;
            }
            QCheckBox {
                color: #F0E6D2;
                font-size: 12px;
                font-weight: bold;
                spacing: 8px;
                background: transparent;
            }
            QCheckBox::indicator {
                width: 15px;
                height: 15px;
                border: 1px solid #C8AA6E;
                background-color: #040E1A;
                border-radius: 2px;
            }
            QCheckBox::indicator:checked {
                background-color: #0AC8B9;
                border-color: #A3F5EE;
            }
            """
        )

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        frame = QFrame()
        frame.setObjectName("OuterFrame")
        outer.addWidget(frame)

        root = QVBoxLayout(frame)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QFrame()
        header.setObjectName("HeaderBar")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(18, 12, 16, 12)
        hl.setSpacing(10)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title_lbl = QLabel("◈ 问题反馈与运行日志上报")
        title_lbl.setFont(display_font(13, bold=True, letter_spacing=1.0))
        title_lbl.setStyleSheet("color: #F0E6D2; background: transparent; border: none;")
        sub_lbl = QLabel(
            f"v{APP_VERSION} · 本地自动滚动保留最近 {self._log_ring.max_matches} 局日志，超出自动淘汰最旧记录并脱敏回传"
        )
        sub_lbl.setStyleSheet("color: #A09B8C; font-size: 11px; background: transparent; border: none;")
        title_col.addWidget(title_lbl)
        title_col.addWidget(sub_lbl)
        hl.addLayout(title_col, 1)

        acct_tag = QLabel(f"账号: {self._username}" if self._username else "匿名直连上报")
        acct_tag.setFont(latin_serif_font(9, bold=True))
        acct_tag.setFixedHeight(26)
        acct_tag.setStyleSheet(
            "color: #0AC8B9; background-color: rgba(10, 200, 185, 22); "
            "border: 1px solid rgba(10, 200, 185, 110); border-radius: 2px; padding: 2px 10px;"
        )
        hl.addWidget(acct_tag, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(header)

        # Body
        body = QVBoxLayout()
        body.setContentsMargins(18, 14, 18, 14)
        body.setSpacing(11)

        cat_lbl = QLabel("1. 选择问题类型")
        cat_lbl.setObjectName("SectionLbl")
        body.addWidget(cat_lbl)

        cat_row = QHBoxLayout()
        cat_row.setSpacing(6)
        for code, label in FEEDBACK_CATEGORIES:
            btn = QPushButton(label)
            btn.setObjectName("CatPill")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, c=code: self._select_category(c))
            self._cat_buttons[code] = btn
            cat_row.addWidget(btn)
        cat_row.addStretch(1)
        body.addLayout(cat_row)
        self._select_category("ocr_picker")

        msg_lbl = QLabel("2. 描述遇到的问题（必填）")
        msg_lbl.setObjectName("SectionLbl")
        body.addWidget(msg_lbl)

        self.message_edit = QTextEdit()
        self.message_edit.setFont(body_font(10))
        self.message_edit.setPlaceholderText(
            "请简述你遇到的异常或建议（例如：哪一局、什么英雄、选卡识别漏了哪张海克斯，或浮窗在什么情况下卡住）..."
        )
        self.message_edit.setMinimumHeight(112)
        self.message_edit.setMaximumHeight(150)
        body.addWidget(self.message_edit)

        contact_lbl = QLabel("3. 联系方式（选填，方便排查后回复或补偿积分）")
        contact_lbl.setObjectName("SectionLbl")
        body.addWidget(contact_lbl)

        self.contact_edit = QLineEdit()
        self.contact_edit.setPlaceholderText("选填：QQ / 微信 / 邮箱")
        self.contact_edit.setMaxLength(128)
        body.addWidget(self.contact_edit)

        # Rolling log attachment card
        log_card = QFrame()
        log_card.setObjectName("LogCard")
        lc = QVBoxLayout(log_card)
        lc.setContentsMargins(12, 10, 12, 10)
        lc.setSpacing(7)

        top_chk_row = QHBoxLayout()
        self.attach_logs_chk = QCheckBox("附带最近滚动运行日志与环境诊断（已自动脱敏令牌、密码与用户名）")
        self.attach_logs_chk.setChecked(True)
        top_chk_row.addWidget(self.attach_logs_chk, 1)
        lc.addLayout(top_chk_row)

        self.log_summary_lbl = QLabel("正在统计本地 20 局滚动日志...")
        self.log_summary_lbl.setStyleSheet("color: #A09B8C; font-size: 11px; background: transparent; border: none;")
        self.log_summary_lbl.setWordWrap(True)
        lc.addWidget(self.log_summary_lbl)

        mini_row = QHBoxLayout()
        mini_row.setSpacing(8)
        self.preview_btn = QPushButton("预览脱敏日志")
        self.preview_btn.setObjectName("MiniLinkBtn")
        self.preview_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview_btn.clicked.connect(self._toggle_preview)

        self.open_dir_btn = QPushButton("打开日志目录")
        self.open_dir_btn.setObjectName("MiniLinkBtn")
        self.open_dir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_dir_btn.clicked.connect(self._open_log_folder)

        self.export_btn = QPushButton("导出本地日志包")
        self.export_btn.setObjectName("MiniLinkBtn")
        self.export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_btn.clicked.connect(self._export_local_bundle)

        mini_row.addWidget(self.preview_btn)
        mini_row.addWidget(self.open_dir_btn)
        mini_row.addWidget(self.export_btn)
        mini_row.addStretch(1)
        lc.addLayout(mini_row)

        self.preview_box = QPlainTextEdit()
        self.preview_box.setReadOnly(True)
        self.preview_box.setVisible(False)
        self.preview_box.setMinimumHeight(130)
        self.preview_box.setMaximumHeight(170)
        self.preview_box.setStyleSheet(
            "QPlainTextEdit { font-family: 'Cascadia Mono', Consolas, monospace; font-size: 10px; "
            "color: #CBD5E1; background-color: #020810; border: 1px solid rgba(10, 200, 185, 85); }"
        )
        lc.addWidget(self.preview_box)
        body.addWidget(log_card)

        # Success ticket banner (hidden until submitted)
        self.success_card = QFrame()
        self.success_card.setObjectName("SuccessCard")
        self.success_card.setVisible(False)
        sc = QHBoxLayout(self.success_card)
        sc.setContentsMargins(12, 9, 12, 9)
        sc.setSpacing(10)
        self.success_lbl = QLabel("")
        self.success_lbl.setWordWrap(True)
        self.success_lbl.setStyleSheet("color: #E2F8F5; font-size: 12px; background: transparent; border: none;")
        self.copy_ticket_btn = QPushButton("复制单号")
        self.copy_ticket_btn.setObjectName("MiniLinkBtn")
        self.copy_ticket_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_ticket_btn.clicked.connect(self._copy_ticket)
        sc.addWidget(self.success_lbl, 1)
        sc.addWidget(self.copy_ticket_btn)
        body.addWidget(self.success_card)

        # Inline status/error bar
        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color: #F59E0B; font-size: 11px; background: transparent; border: none;")
        body.addWidget(self.status_lbl)

        body.addStretch(1)

        # Footer buttons
        foot = QHBoxLayout()
        foot.setSpacing(10)
        foot.addStretch(1)
        self.cancel_btn = HextechActionButton("关闭", variant="gold", height=34)
        self.cancel_btn.setMinimumWidth(92)
        self.cancel_btn.clicked.connect(self.reject)

        self.submit_btn = HextechActionButton("提交反馈并回传日志", variant="cyan", pointed_right=True, height=34)
        self.submit_btn.setMinimumWidth(186)
        self.submit_btn.clicked.connect(self._handle_submit)

        foot.addWidget(self.cancel_btn)
        foot.addWidget(self.submit_btn)
        body.addLayout(foot)

        root.addLayout(body, 1)

    def _select_category(self, code: str) -> None:
        self._selected_category = code
        for k, btn in self._cat_buttons.items():
            btn.setProperty("active", "true" if k == code else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _ensure_bundle(self) -> tuple[str, int, str]:
        if self._cached_bundle_text is None:
            text, count, _stats = self._log_ring.build_feedback_bundle()
            self._cached_bundle_text = text
            self._cached_bundle_matches = count
            self._cached_bundle_gz_b64 = compress_log_bundle(text)
        return self._cached_bundle_text, self._cached_bundle_matches, self._cached_bundle_gz_b64

    def _refresh_log_summary(self) -> None:
        stats = self._log_ring.summary_stats()
        text, included, gz_b64 = self._ensure_bundle()
        raw_kb = len(text.encode("utf-8", errors="replace")) / 1024.0
        gz_kb = len(gz_b64.encode("ascii")) / 1024.0
        self.log_summary_lbl.setText(
            f"滚动日志池：{stats['match_count']} / {stats['max_matches']} 局"
            f"（异常 {stats['total_errors']} 条 · 告警 {stats['total_warns']} 条） · "
            f"打包 {included} 局约 {raw_kb:.1f} KB（压缩回传仅 {gz_kb:.1f} KB）"
        )

    def _toggle_preview(self) -> None:
        show = not self.preview_box.isVisible()
        if show:
            text, _, _ = self._ensure_bundle()
            # Display tail/overview cleanly without stalling QPlainTextEdit on huge text
            preview = text if len(text) <= 40_000 else (text[:12_000] + "\n\n... [中间略去，完整日志将随提交一并压缩上传] ...\n\n" + text[-24_000:])
            self.preview_box.setPlainText(preview)
            self.preview_btn.setText("收起日志预览")
        else:
            self.preview_btn.setText("预览脱敏日志")
        self.preview_box.setVisible(show)

    def _open_log_folder(self) -> None:
        folder = Path(self._log_ring.root_dir)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(folder))  # type: ignore[attr-defined]
        except Exception as exc:
            self.status_lbl.setText(f"无法打开日志目录：{exc}")

    def _export_local_bundle(self) -> None:
        text, _, _ = self._ensure_bundle()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = str(self._log_ring.root_dir.parent / f"HexHelper_LogBundle_{stamp}.log")
        dest, _ = QFileDialog.getSaveFileName(
            self,
            "导出脱敏运行日志包",
            default_name,
            "Log Files (*.log *.txt);;All Files (*.*)",
        )
        if not dest:
            return
        try:
            Path(dest).write_text(text, encoding="utf-8")
            self.status_lbl.setStyleSheet("color: #0AC8B9; font-size: 11px; background: transparent; border: none;")
            self.status_lbl.setText(f"已导出本地脱敏日志包：{dest}")
        except Exception as exc:
            self.status_lbl.setStyleSheet("color: #FCA5A5; font-size: 11px; background: transparent; border: none;")
            self.status_lbl.setText(f"导出失败：{exc}")

    def _copy_ticket(self) -> None:
        if not self._last_ticket_no:
            return
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(self._last_ticket_no)
        self.copy_ticket_btn.setText("已复制 ✓")

    def _handle_submit(self) -> None:
        if self._submitting:
            return
        message = self.message_edit.toPlainText().strip()
        if len(message) < 2:
            self.status_lbl.setStyleSheet("color: #F59E0B; font-size: 11px; background: transparent; border: none;")
            self.status_lbl.setText("请至少输入 2 个字描述遇到的问题或建议。")
            self.message_edit.setFocus()
            return

        attach_logs = self.attach_logs_chk.isChecked()
        log_gz_b64 = ""
        match_count = 0
        if attach_logs:
            # Refresh fresh bundle right before submit so latest log lines are captured
            self._cached_bundle_text = None
            _, match_count, log_gz_b64 = self._ensure_bundle()

        payload: dict[str, Any] = {
            "category": self._selected_category,
            "message": message,
            "contact": self.contact_edit.text().strip(),
            "dataset_patch": self._dataset_patch,
            "dataset_mode": self._dataset_mode,
            "os_info": f"{platform.system()} {platform.release()} ({platform.machine()})",
            "match_count": match_count if attach_logs else 0,
            "diag": dict(self._diag_snapshot),
            "log_gzip_b64": log_gz_b64 if attach_logs else "",
        }

        self._submitting = True
        self.submit_btn.setEnabled(False)
        self.submit_btn.setText("正在回传日志...")
        self.status_lbl.setStyleSheet("color: #0AC8B9; font-size: 11px; background: transparent; border: none;")
        self.status_lbl.setText("正在免代理直连回传反馈与脱敏运行日志，请稍候...")

        def _worker() -> None:
            try:
                res = self._submit_fn(payload)
                self._bridge.succeeded.emit(res if isinstance(res, dict) else {"ticket_no": ""})
            except Exception as exc:
                self._bridge.failed.emit(str(exc))

        threading.Thread(target=_worker, name="HexFeedbackSubmit", daemon=True).start()

    def _on_submit_succeeded(self, res: dict[str, Any]) -> None:
        self._submitting = False
        ticket_no = str(res.get("ticket_no") or "").strip()
        self._last_ticket_no = ticket_no
        log_kb = int(res.get("log_bytes") or 0) / 1024.0
        self.submit_btn.setEnabled(False)
        self.submit_btn.setText("已提交 ✓")
        self.cancel_btn.setText("完成并关闭")
        self.status_lbl.setText("")
        self.success_lbl.setText(
            f"反馈已成功回传至开发后台！工单号：{ticket_no or '已记录'}"
            + (f"（已附带 {log_kb:.1f} KB 脱敏日志）" if log_kb > 0 else "")
        )
        self.copy_ticket_btn.setVisible(bool(ticket_no))
        self.copy_ticket_btn.setText("复制单号")
        self.success_card.setVisible(True)

    def _on_submit_failed(self, err_msg: str) -> None:
        self._submitting = False
        self.submit_btn.setEnabled(True)
        self.submit_btn.setText("重新提交反馈")
        self.status_lbl.setStyleSheet("color: #FCA5A5; font-size: 11px; background: transparent; border: none;")
        self.status_lbl.setText(
            f"回传失败：{err_msg}（可稍后重试，或点击上方「导出本地日志包」手动发送）"
        )
