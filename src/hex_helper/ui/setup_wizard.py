"""Hextech-styled First-Run Environment Setup & Preflight Wizard Dialog."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from hex_helper.config import AppConfig, save_config
from hex_helper.edition import APP_NAME, APP_VERSION
from hex_helper.game.lol_locator import clear_locator_cache, validate_lol_install_dir
from hex_helper.setup.preflight import (
    PreflightItem,
    PreflightReport,
    run_windows_preflight,
    trigger_install_zh_ocr_pack,
)
from hex_helper.ui.hextech_widgets import HextechActionButton
from hex_helper.ui.theme import body_font, display_font, load_app_icon, load_app_pixmap


class SetupWizardDialog(QDialog):
    """First-run Hextech dark dependency check & LoL installation setup dialog."""

    def __init__(
        self,
        cfg: AppConfig,
        report: PreflightReport,
        data_dir: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.report = report
        self.data_dir = Path(data_dir)
        self.selected_lockfile_path: str = (
            str(report.lol_install.lockfile_path)
            if report.lol_install is not None
            else (cfg.lockfile_path or "")
        )

        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION} · 首次运行环境向导")
        self.setWindowIcon(load_app_icon())
        self.setMinimumWidth(580)
        self.setMaximumWidth(740)
        self.setModal(True)
        self.setStyleSheet(
            "QDialog { background-color: #040E1A; color: #F0E6D2; "
            "border: 1px solid #C8AA6E; }"
            "QLabel { background: transparent; color: #F0E6D2; }"
        )

        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(20, 18, 20, 18)
        root_lay.setSpacing(12)

        # Header
        hdr = QHBoxLayout()
        hdr.setSpacing(12)
        emblem = QLabel()
        emblem.setFixedSize(42, 42)
        emblem.setPixmap(load_app_pixmap(40))
        hdr.addWidget(emblem)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title_lbl = QLabel(f"HEXHELPER v{APP_VERSION} · 运行环境自检与客户端定位")
        title_lbl.setFont(display_font(13, bold=True, letter_spacing=1.2))
        title_lbl.setStyleSheet("color: #F0E6D2;")
        sub_lbl = QLabel("自动检测系统依赖、内置 26.23 离线数据包与国服《英雄联盟》路径")
        sub_lbl.setFont(body_font(10))
        sub_lbl.setStyleSheet("color: #0AC8B9;")
        title_col.addWidget(title_lbl)
        title_col.addWidget(sub_lbl)
        hdr.addLayout(title_col, 1)
        root_lay.addLayout(hdr)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background-color: #785A28;")
        root_lay.addWidget(sep)

        # 4 preflight item rows container
        self._rows_container = QWidget()
        self._rows_container.setStyleSheet("background: transparent;")
        self._rows_lay = QVBoxLayout(self._rows_container)
        self._rows_lay.setContentsMargins(0, 2, 0, 2)
        self._rows_lay.setSpacing(8)
        root_lay.addWidget(self._rows_container)

        # Inline status feedback label
        self.feedback_lbl = QLabel("")
        self.feedback_lbl.setWordWrap(True)
        self.feedback_lbl.setFont(body_font(10))
        self.feedback_lbl.setStyleSheet("color: #A09B8C; padding: 2px 4px;")
        root_lay.addWidget(self.feedback_lbl)

        # Action bar (compact labels so width stays <=660px at 125%-150% DPI!)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        self.rescan_btn = HextechActionButton("重扫 LOL", variant="gold", height=34)
        self.rescan_btn.clicked.connect(self.rescan)

        self.browse_lol_btn = HextechActionButton("选 LOL 目录", variant="gold", height=34)
        self.browse_lol_btn.clicked.connect(self._browse_lol_dir)

        self.install_ocr_btn = HextechActionButton("补装中文 OCR", variant="cyan", height=34)
        self.install_ocr_btn.clicked.connect(self._install_ocr_clicked)

        self.save_btn = HextechActionButton(
            "保存并进入",
            variant="cyan",
            pointed_right=True,
            height=36,
        )
        self.save_btn.clicked.connect(self._save_and_accept)

        btn_row.addWidget(self.rescan_btn)
        btn_row.addWidget(self.browse_lol_btn)
        btn_row.addWidget(self.install_ocr_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.save_btn)
        root_lay.addLayout(btn_row)

        self._render_rows()

    def _clear_rows(self) -> None:
        while self._rows_lay.count():
            item = self._rows_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _render_rows(self) -> None:
        self._clear_rows()
        for item in self.report.items:
            row = QFrame()
            if item.ok:
                border_c = "#09C269"
                badge_txt = "✓"
                badge_fg = "#09C269"
                badge_bg = "rgba(9, 194, 105, 0.16)"
            elif not item.critical:
                border_c = "#FBBF24"
                badge_txt = "!"
                badge_fg = "#FBBF24"
                badge_bg = "rgba(251, 191, 36, 0.16)"
            else:
                border_c = "#EF4444"
                badge_txt = "✕"
                badge_fg = "#EF4444"
                badge_bg = "rgba(239, 68, 68, 0.18)"

            row.setStyleSheet(
                f"QFrame {{ background-color: #091428; border: 1px solid #463714; "
                f"border-left: 3px solid {border_c}; border-radius: 2px; }}"
            )
            rl = QHBoxLayout(row)
            rl.setContentsMargins(12, 9, 12, 9)
            rl.setSpacing(10)

            badge = QLabel(badge_txt)
            badge.setFixedSize(26, 26)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setFont(display_font(12, bold=True))
            badge.setStyleSheet(
                f"background-color: {badge_bg}; color: {badge_fg}; "
                f"border: 1px solid {border_c}; border-radius: 2px;"
            )
            rl.addWidget(badge)

            col = QVBoxLayout()
            col.setSpacing(2)
            lbl = QLabel(item.label)
            lbl.setFont(display_font(11, bold=True, letter_spacing=0.6))
            lbl.setStyleSheet("border: none; color: #F0E6D2;")
            det = QLabel(item.detail)
            det.setWordWrap(True)
            det.setFont(body_font(10))
            det.setStyleSheet("border: none; color: #A09B8C;")
            col.addWidget(lbl)
            col.addWidget(det)
            if not item.ok and item.fix_hint:
                hint = QLabel(f"建议：{item.fix_hint}")
                hint.setWordWrap(True)
                hint.setFont(body_font(9))
                hint.setStyleSheet(f"border: none; color: {badge_fg};")
                col.addWidget(hint)

            rl.addLayout(col, 1)
            self._rows_lay.addWidget(row)

        ocr_item = self.report.get("winrt_ocr")
        ocr_failed = ocr_item is not None and not ocr_item.ok
        self.install_ocr_btn.setVisible(ocr_failed)

    def apply_manual_lol_dir(self, chosen_dir: str | Path) -> bool:
        """Validate a manually selected LoL installation directory and update lockfile state."""
        info = validate_lol_install_dir(Path(chosen_dir), source="manual")
        if info is None:
            self.feedback_lbl.setText(
                "✕ 所选目录中未检测到 LeagueClient.exe 或 League of Legends.exe，请重新选择《英雄联盟》根目录"
            )
            self.feedback_lbl.setStyleSheet("color: #EF4444; padding: 2px 4px;")
            return False

        self.selected_lockfile_path = str(info.lockfile_path)
        self.cfg.lockfile_path = self.selected_lockfile_path
        self.cfg.lol_install_dir = str(info.install_dir)
        self.report.lol_install = info
        updated: list[PreflightItem] = []
        replaced = False
        for it in self.report.items:
            if it.key == "lol_path":
                updated.append(
                    PreflightItem(
                        key="lol_path",
                        label=it.label,
                        ok=True,
                        detail=f"已手动定位: {info.install_dir} [manual]",
                        fix_hint="",
                        critical=False,
                    )
                )
                replaced = True
            else:
                updated.append(it)
        if not replaced:
            updated.append(
                PreflightItem(
                    key="lol_path",
                    label="英雄联盟客户端路径",
                    ok=True,
                    detail=f"已手动定位: {info.install_dir} [manual]",
                    critical=False,
                )
            )
        self.report.items = updated
        self._render_rows()
        self.feedback_lbl.setText(f"✓ 已验证客户端目录：{info.install_dir}")
        self.feedback_lbl.setStyleSheet("color: #09C269; padding: 2px 4px;")
        return True

    def _browse_lol_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择《英雄联盟》安装根目录 (含 LeagueClient)")
        if chosen:
            self.apply_manual_lol_dir(chosen)

    def _install_ocr_clicked(self) -> None:
        ok, msg = trigger_install_zh_ocr_pack()
        self.feedback_lbl.setText(("✓ " if ok else "! ") + msg)
        self.feedback_lbl.setStyleSheet(
            f"color: {'#0AC8B9' if ok else '#FBBF24'}; padding: 2px 4px;"
        )

    def rescan(self) -> PreflightReport:
        """Clear locator cache and re-run all Windows preflight checks."""
        clear_locator_cache()
        self.report = run_windows_preflight(self.cfg, self.data_dir)
        if self.report.lol_install is not None:
            self.selected_lockfile_path = str(self.report.lol_install.lockfile_path)
        elif self.cfg.lockfile_path:
            self.selected_lockfile_path = self.cfg.lockfile_path
        self._render_rows()
        self.feedback_lbl.setText("✓ 已完成重新检测")
        self.feedback_lbl.setStyleSheet("color: #0AC8B9; padding: 2px 4px;")
        return self.report

    def _save_and_accept(self) -> None:
        if self.selected_lockfile_path:
            self.cfg.lockfile_path = self.selected_lockfile_path
        if self.report.lol_install is not None:
            self.cfg.lol_install_dir = str(self.report.lol_install.install_dir)
        self.cfg.first_run_completed = True
        save_config(self.cfg, self.data_dir / "config.json")
        self.accept()
