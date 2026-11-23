"""Hextech-styled Login/Register and Personal QR Recharge dialogs for Commercial Edition."""

from __future__ import annotations

import io
import math
from pathlib import Path
import re
import threading
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from hex_helper.cloud.client import CloudCoachError, CloudGatewayClient
from hex_helper.cloud.updater import UpdateInfo, UpdateVerificationError, download_and_verify_update
from hex_helper.ui.hextech_widgets import HextechActionButton
from hex_helper.ui.theme import BASE_OVERLAY_QSS, body_font, display_font, load_app_icon
from hex_helper.ui.win32_window import UiBridgeInvoker

ESTIMATED_CREDITS_PER_MATCH: dict[str, float] = {
    "a": 220.0,
    "auto": 320.0,
    "b": 1100.0,
}

_LEGACY_MATCH_SUFFIX_RE = re.compile(r"\s*[（(]\s*约[^）)]*局\s*[）)]\s*$")
_LABEL_CREDITS_RE = re.compile(r"([\d,]+)\s*积分")


def _normalize_tier(tier: str | None) -> str:
    norm = (tier or "auto").strip().lower()
    return norm if norm in ESTIMATED_CREDITS_PER_MATCH else "auto"


def estimate_remaining_matches(balance_credits: float, tier: str = "auto") -> int:
    """Estimate remaining full ARAM matches from credit balance without taximeter anxiety."""
    try:
        bal = float(balance_credits or 0.0)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(bal) or bal <= 0.0:
        return 0
    per_match = ESTIMATED_CREDITS_PER_MATCH[_normalize_tier(tier)]
    est = max(0, int(bal // per_match))
    if est == 0 and bal >= 50.0:
        return 1
    return est


def format_balance_match_friendly(balance_credits: float, tier: str = "auto") -> str:
    """Format credit balance leading with remaining matches, e.g. '约剩 15 局 (4,850 积分)'."""
    try:
        bal = float(balance_credits or 0.0)
    except (TypeError, ValueError):
        bal = 0.0
    if not math.isfinite(bal) or bal < 0.0:
        bal = 0.0
    matches = estimate_remaining_matches(bal, tier=tier)
    return f"约剩 {matches} 局 ({bal:,.0f} 积分)"


def format_package_unit_hint(amount_fen: int, credits: int, tier: str = "auto") -> str:
    """Format recharge package match estimate & per-match cost, e.g. '约 16 局 · 约 3 毛/局 · 永不过期'."""
    try:
        fen = int(amount_fen or 0)
        cr = int(credits or 0)
    except (TypeError, ValueError):
        return "永不过期"
    if cr <= 0:
        return "永不过期"
    per_match = ESTIMATED_CREDITS_PER_MATCH[_normalize_tier(tier)]
    matches = max(1, int(round(cr / per_match)))
    if fen <= 0:
        return f"约 {matches} 局 · 永不过期"
    yuan_per_match = (fen / 100.0) / float(matches)
    if yuan_per_match >= 0.95:
        rounded_yuan = max(1.0, round(yuan_per_match, 1))
        price_txt = f"约 {rounded_yuan:.1f} 元/局".replace(".0 元/局", " 元/局")
    else:
        jiao = yuan_per_match * 10.0
        if jiao < 1.0:
            price_txt = "不足 1 毛/局"
        else:
            price_txt = f"约 {int(round(jiao))} 毛/局"
    return f"约 {matches} 局 · {price_txt} · 永不过期"


class _PackageOptionCard(QFrame):
    """Clickable Hextech recharge package row pairing price/title, optional anchor tag, and unit-cost pill."""

    def __init__(
        self,
        radio: QRadioButton,
        hint_text: str,
        parent: QWidget | None = None,
        *,
        badge_text: str = "",
        badge_gold: bool = False,
    ) -> None:
        super().__init__(parent)
        self.radio = radio
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(34)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 3, 8, 3)
        row.setSpacing(6)

        self.radio.setStyleSheet(
            "QRadioButton { color: #F0E6D2; font-size: 11px; font-weight: bold; background: transparent; border: none; spacing: 6px; }"
        )
        row.addWidget(self.radio)

        self.badge_lbl: QLabel | None = None
        if badge_text:
            self.badge_lbl = QLabel(badge_text)
            self.badge_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if badge_gold:
                self.badge_lbl.setStyleSheet(
                    "QLabel { background-color: rgba(200, 155, 60, 48); border: 1px solid #C8AA6E; "
                    "border-radius: 2px; padding: 0px 5px; color: #FDE68A; font-size: 9px; font-weight: bold; }"
                )
            else:
                self.badge_lbl.setStyleSheet(
                    "QLabel { background-color: rgba(10, 200, 185, 30); border: 1px solid #0397AB; "
                    "border-radius: 2px; padding: 0px 5px; color: #5EEAD4; font-size: 9px; font-weight: bold; }"
                )
            row.addWidget(self.badge_lbl)

        row.addStretch(1)

        self.hint_pill = QLabel(hint_text)
        self.hint_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.hint_pill)

        self.set_selected(self.radio.isChecked())

    def set_selected(self, selected: bool) -> None:
        if selected:
            self.setStyleSheet(
                "_PackageOptionCard { background-color: rgba(10, 42, 62, 185); "
                "border: 1px solid #0AC8B9; border-radius: 2px; }"
            )
            self.hint_pill.setStyleSheet(
                "QLabel { background-color: rgba(10, 200, 185, 34); border: 1px solid #0AC8B9; "
                "border-radius: 2px; padding: 1px 7px; color: #5EEAD4; font-size: 10px; font-weight: bold; }"
            )
        else:
            self.setStyleSheet(
                "_PackageOptionCard { background-color: rgba(9, 20, 40, 205); "
                "border: 1px solid #463714; border-radius: 2px; }"
                "_PackageOptionCard:hover { border-color: #C8AA6E; }"
            )
            self.hint_pill.setStyleSheet(
                "QLabel { background-color: rgba(200, 170, 110, 20); border: 1px solid rgba(200, 170, 110, 145); "
                "border-radius: 2px; padding: 1px 7px; color: #F0E6D2; font-size: 10px; font-weight: bold; }"
            )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.radio.isEnabled():
            self.radio.setChecked(True)
        super().mousePressEvent(event)


def _render_qr_payload_pixmap(payload: str, size: int = 196) -> QPixmap | None:
    """Render a QR string locally using qrcode + Pillow if server uploaded no static image."""
    txt = (payload or "").strip()
    if not txt:
        return None
    try:
        import qrcode

        img = qrcode.make(txt)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        pm = QPixmap()
        if pm.loadFromData(buf.getvalue()):
            return pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
    except Exception:
        pass
    return None


class LoginRegisterDialog(QDialog):
    """Hextech Dark login & registration modal dialog backed by CloudGatewayClient."""

    def __init__(
        self,
        cloud_client: CloudGatewayClient,
        parent: QWidget | None = None,
        initial_mode: str = "login",
    ) -> None:
        super().__init__(parent)
        self._client = cloud_client
        self._mode = "register" if initial_mode == "register" else "login"
        self._invoker = UiBridgeInvoker()
        self._busy = False
        self._closed = False
        self.auth_result: dict[str, Any] | None = None
        self.was_registration: bool = False
        self.signup_bonus_credits: float = 0.0

        self.setWindowTitle("HexHelper · 账号登录与注册")
        self.setWindowIcon(load_app_icon())
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setStyleSheet(
            BASE_OVERLAY_QSS
            + """
            QDialog {
                background-color: #040E1A;
                border: 1px solid #C8AA6E;
            }
            QRadioButton {
                color: #F0E6D2;
                spacing: 8px;
            }
            QRadioButton::indicator {
                width: 14px;
                height: 14px;
                border-radius: 7px;
                border: 1px solid #C8AA6E;
                background-color: #010A13;
            }
            QRadioButton::indicator:checked {
                border: 2px solid #0AC8B9;
                background-color: #0AC8B9;
            }
            """
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)

        title = QLabel("HEXHELPER 云端通行证")
        title.setFont(display_font(14, bold=True, letter_spacing=1.2))
        title.setStyleSheet("color: #F0E6D2; background: transparent; border: none;")
        sub = QLabel("登录后即可启用云端 AI 实时出装与选牌推演（本地 5ms 大数据查表永久免费）")
        sub.setWordWrap(True)
        sub.setFont(body_font(10))
        sub.setStyleSheet("color: #0AC8B9; background: transparent; border: none;")
        lay.addWidget(title)
        lay.addWidget(sub)

        # Mode switch bar
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self.mode_login_btn = QPushButton("登录已有账号")
        self.mode_register_btn = QPushButton("注册新账号")
        for btn in (self.mode_login_btn, self.mode_register_btn):
            btn.setFixedHeight(30)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mode_login_btn.clicked.connect(lambda: self.set_mode("login"))
        self.mode_register_btn.clicked.connect(lambda: self.set_mode("register"))
        mode_row.addWidget(self.mode_login_btn, 1)
        mode_row.addWidget(self.mode_register_btn, 1)
        lay.addLayout(mode_row)

        # Form inputs
        u_lbl = QLabel("账号名（3-24 位字母、数字、下划线或中文）")
        u_lbl.setStyleSheet("color: #C8AA6E; font-size: 11px; background: transparent; border: none;")
        self.username_edit = QLineEdit()
        self.username_edit.setFixedHeight(34)
        self.username_edit.setPlaceholderText("输入账号名...")

        p_lbl = QLabel("登录密码（至少 6 位）")
        p_lbl.setStyleSheet("color: #C8AA6E; font-size: 11px; background: transparent; border: none;")
        pwd_row = QHBoxLayout()
        pwd_row.setSpacing(6)
        self.password_edit = QLineEdit()
        self.password_edit.setFixedHeight(34)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("输入密码...")
        self.toggle_pwd_btn = QPushButton("显示")
        self.toggle_pwd_btn.setFixedSize(56, 34)
        self.toggle_pwd_btn.clicked.connect(self._toggle_pwd_vis)
        pwd_row.addWidget(self.password_edit, 1)
        pwd_row.addWidget(self.toggle_pwd_btn)

        self.confirm_lbl = QLabel("确认密码")
        self.confirm_lbl.setStyleSheet("color: #C8AA6E; font-size: 11px; background: transparent; border: none;")
        self.confirm_password_edit = QLineEdit()
        self.confirm_password_edit.setFixedHeight(34)
        self.confirm_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_password_edit.setPlaceholderText("再次输入密码...")

        self.register_bonus_hint_lbl = QLabel(
            "✦ 新用户注册即赠 450 积分（约 1.5-2 局智能 AI 体验），免代理直连 · 本地 5ms 查表永久免费"
        )
        self.register_bonus_hint_lbl.setWordWrap(True)
        self.register_bonus_hint_lbl.setStyleSheet(
            "background-color: rgba(10, 200, 185, 22); border: 1px solid rgba(10, 200, 185, 110); "
            "border-radius: 2px; padding: 6px 10px; color: #5EEAD4; font-size: 11px;"
        )

        lay.addWidget(u_lbl)
        lay.addWidget(self.username_edit)
        lay.addWidget(p_lbl)
        lay.addLayout(pwd_row)
        lay.addWidget(self.confirm_lbl)
        lay.addWidget(self.confirm_password_edit)
        lay.addWidget(self.register_bonus_hint_lbl)

        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color: #A09B8C; font-size: 11px; background: transparent; border: none;")
        lay.addWidget(self.status_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self.cancel_btn = HextechActionButton("取消", variant="gold", height=34)
        self.cancel_btn.clicked.connect(self.reject)
        self.submit_btn = HextechActionButton("登录 HexHelper", variant="cyan", pointed_right=True, height=34)
        self.submit_btn.clicked.connect(self._submit)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.submit_btn)
        lay.addLayout(btn_row)

        self.username_edit.returnPressed.connect(self._submit)
        self.password_edit.returnPressed.connect(self._submit)
        self.confirm_password_edit.returnPressed.connect(self._submit)

        self.set_mode(self._mode)

    def _toggle_pwd_vis(self) -> None:
        show = self.password_edit.echoMode() == QLineEdit.EchoMode.Password
        mode = QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
        self.password_edit.setEchoMode(mode)
        self.confirm_password_edit.setEchoMode(mode)
        self.toggle_pwd_btn.setText("隐藏" if show else "显示")

    def set_mode(self, mode: str) -> None:
        self._mode = "register" if mode == "register" else "login"
        is_reg = self._mode == "register"
        self.confirm_lbl.setVisible(is_reg)
        self.confirm_password_edit.setVisible(is_reg)
        self.register_bonus_hint_lbl.setVisible(is_reg)
        self.submit_btn.setText("注册并登录" if is_reg else "登录 HexHelper")
        self.status_lbl.setText("")

        active_qss = (
            "QPushButton { background-color: rgba(10, 200, 185, 55); border: 1px solid #0AC8B9; "
            "color: #F0E6D2; font-weight: bold; font-size: 12px; border-radius: 2px; }"
        )
        idle_qss = (
            "QPushButton { background-color: #091428; border: 1px solid #463714; "
            "color: #A09B8C; font-size: 12px; border-radius: 2px; }"
            "QPushButton:hover { border-color: #C8AA6E; color: #F0E6D2; }"
        )
        self.mode_login_btn.setStyleSheet(idle_qss if is_reg else active_qss)
        self.mode_register_btn.setStyleSheet(active_qss if is_reg else idle_qss)

    def _set_status(self, text: str, *, color: str = "#A09B8C") -> None:
        self.status_lbl.setText(text)
        self.status_lbl.setStyleSheet(f"color: {color}; font-size: 11px; background: transparent; border: none;")

    def _submit(self) -> None:
        if self._busy:
            return
        username = self.username_edit.text().strip()
        password = self.password_edit.text()
        if len(username) < 3:
            self._set_status("✕ 账号名长度需为 3-24 个字符", color="#F87171")
            return
        if len(password) < 6:
            self._set_status("✕ 密码长度至少为 6 位", color="#F87171")
            return
        if self._mode == "register" and password != self.confirm_password_edit.text():
            self._set_status("✕ 两次输入的密码不一致", color="#F87171")
            return

        self._busy = True
        self.submit_btn.setEnabled(False)
        self._set_status("⏳ 正在连接云端验证身份...", color="#FBBF24")
        mode = self._mode

        def worker() -> None:
            try:
                if mode == "register":
                    res = self._client.register(username, password)
                else:
                    res = self._client.login(username, password)
                self._invoker.invoke.emit(lambda r=res, m=mode: self._on_auth_ok(r, mode=m))
            except CloudCoachError as exc:
                self._invoker.invoke.emit(lambda m=exc.message: self._on_auth_fail(m))
            except Exception as exc:
                self._invoker.invoke.emit(lambda m=str(exc): self._on_auth_fail(m))

        threading.Thread(target=worker, name="HexCloudAuth", daemon=True).start()

    def _on_auth_ok(self, res: dict[str, Any], *, mode: str = "login") -> None:
        self._busy = False
        if self._closed:
            return
        self.submit_btn.setEnabled(True)
        self.auth_result = res
        self.was_registration = mode == "register"
        if self.was_registration:
            raw_bonus = res.get("signup_bonus_credits")
            if raw_bonus is None:
                raw_bonus = (res.get("user") or {}).get("balance_credits")
            try:
                self.signup_bonus_credits = max(0.0, float(raw_bonus or 0.0))
            except (TypeError, ValueError):
                self.signup_bonus_credits = 0.0
        else:
            self.signup_bonus_credits = 0.0
        self._set_status("✓ 登录成功，已加密保存本地凭证", color="#09C269")
        self.accept()

    def _on_auth_fail(self, msg: str) -> None:
        self._busy = False
        if self._closed:
            return
        self.submit_btn.setEnabled(True)
        self._set_status(f"✕ {msg}", color="#F87171")

    def reject(self) -> None:
        self._closed = True
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closed = True
        super().closeEvent(event)


class SignupBonusDialog(QDialog):
    """Hextech welcome modal displayed right after a new user registers and receives bonus credits."""

    def __init__(
        self,
        username: str,
        bonus_credits: float,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.username = (username or "").strip()
        self.bonus_credits = max(0.0, float(bonus_credits))
        bonus_str = (
            f"{int(round(self.bonus_credits)):,}"
            if abs(self.bonus_credits - round(self.bonus_credits)) < 1e-6
            else f"{self.bonus_credits:,.1f}"
        )

        self.setWindowTitle("HexHelper · 新用户注册礼遇")
        self.setWindowIcon(load_app_icon())
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setStyleSheet(
            BASE_OVERLAY_QSS
            + """
            QDialog {
                background-color: #040E1A;
                border: 1px solid #C8AA6E;
            }
            """
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)

        est_matches = estimate_remaining_matches(self.bonus_credits, "auto")
        self.badge_lbl = QLabel(f"◈ 注册成功 · 已赠送 {bonus_str} 积分（约 {est_matches} 局体验）")
        self.badge_lbl.setFont(display_font(14, bold=True, letter_spacing=1.2))
        self.badge_lbl.setStyleSheet("color: #0AC8B9; background: transparent; border: none;")
        lay.addWidget(self.badge_lbl)

        card = QFrame(self)
        card.setStyleSheet(
            "QFrame { background-color: #091428; border: 1px solid #C8AA6E; border-radius: 2px; }"
        )
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(6)

        greet = f"欢迎加入 HexHelper，{self.username}！" if self.username else "欢迎加入 HexHelper！"
        self.body_lbl = QLabel(
            f"{greet}\n\n"
            f"系统已自动向你的账户发放 {bonus_str} 积分新用户体验额度（{format_balance_match_friendly(self.bonus_credits, 'auto')}）：\n"
            "• 推荐 Auto「智能混合」模式，开局简报与质变大变阵走顶配旗舰、日常反制走极速模型，约 3 毛/局\n"
            "• 局内抽屉与主界面可随时在 Auto / A 档 / B 档间自由切换，积分永不过期\n"
            "• 基础大数据梯度 / 3-4符文识别 / 海克斯组合路线 永久免费保留"
        )
        self.body_lbl.setWordWrap(True)
        self.body_lbl.setFont(body_font(10))
        self.body_lbl.setStyleSheet("color: #F0E6D2; background: transparent; border: none;")
        cl.addWidget(self.body_lbl)
        lay.addWidget(card)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.ok_btn = HextechActionButton("⚡ 唤醒海克斯 AI 核心", variant="cyan", pointed_right=True, height=34)
        self.ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.ok_btn)
        lay.addLayout(btn_row)


class RechargeDialog(QDialog):
    """Personal WeChat/Alipay QR recharge dialog with 3s order polling and instant AI unlock."""

    def __init__(
        self,
        cloud_client: CloudGatewayClient,
        parent: QWidget | None = None,
        on_paid: Callable[[dict[str, Any]], None] | None = None,
        auto_load: bool = True,
        *,
        tier: str = "auto",
        current_balance: float | None = None,
        in_game: bool = False,
        is_first_recharge: bool = False,
    ) -> None:
        super().__init__(parent)
        self._client = cloud_client
        self._on_paid = on_paid
        self._invoker = UiBridgeInvoker()
        self._tier = _normalize_tier(tier)
        self._in_game = bool(in_game)
        self._is_first_recharge = bool(is_first_recharge)
        self.unseal_requested: bool = False
        self._auto_close_timer = QTimer(self)
        self._auto_close_timer.setSingleShot(True)
        self._auto_close_timer.timeout.connect(self.accept)
        self._balance_credits: float | None = None
        if current_balance is not None:
            try:
                self._balance_credits = max(0.0, float(current_balance))
            except (TypeError, ValueError):
                self._balance_credits = None
        else:
            cached_wallet = getattr(cloud_client, "last_wallet", None)
            if isinstance(cached_wallet, dict) and cached_wallet.get("balance_credits") is not None:
                try:
                    self._balance_credits = max(0.0, float(cached_wallet.get("balance_credits") or 0.0))
                except (TypeError, ValueError):
                    self._balance_credits = None
        self._packages: list[dict[str, Any]] = []
        self._channels: list[dict[str, Any]] = []
        self._pkg_cards: dict[str, _PackageOptionCard] = {}
        self._selected_package_id: str = "pkg_99"
        self._selected_channel: str = "manual_wechat"
        self._current_order_no: str = ""
        self._poll_in_flight = False
        self._paid_notified = False
        self._closed = False

        self.setWindowTitle("HexHelper · 积分充值")
        self.setWindowIcon(load_app_icon())
        self.setModal(True)
        self.setMinimumWidth(480)
        self.setStyleSheet(
            BASE_OVERLAY_QSS
            + """
            QDialog {
                background-color: #040E1A;
                border: 1px solid #C8AA6E;
            }
            QRadioButton {
                color: #F0E6D2;
                spacing: 8px;
            }
            QRadioButton::indicator {
                width: 14px;
                height: 14px;
                border-radius: 7px;
                border: 1px solid #C8AA6E;
                background-color: #010A13;
            }
            QRadioButton::indicator:checked {
                border: 2px solid #0AC8B9;
                background-color: #0AC8B9;
            }
            """
        )

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(3000)
        self._poll_timer.timeout.connect(self._poll_tick)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(8)

        hdr_row = QHBoxLayout()
        hdr_row.setSpacing(10)
        hdr = QLabel("HEXHELPER 积分扫码充值")
        hdr.setFont(display_font(13, bold=True, letter_spacing=1.2))
        hdr.setStyleSheet("color: #F0E6D2; background: transparent; border: none;")
        hdr_row.addWidget(hdr)
        hdr_row.addStretch(1)

        self.balance_lbl = QLabel("")
        self.balance_lbl.setStyleSheet(
            "background-color: rgba(9, 20, 40, 230); border: 1px solid #C8AA6E; "
            "border-radius: 2px; padding: 3px 10px; color: #F0E6D2; font-size: 11px; font-weight: bold;"
        )
        hdr_row.addWidget(self.balance_lbl)
        root.addLayout(hdr_row)
        self._refresh_balance_header()

        self.pricing_lbl = QLabel(
            "智能混合约 3 毛/局  |  按实际 Token 透明计费  |  1,000 积分 = ¥1.00  |  永不过期"
        )
        self.pricing_lbl.setWordWrap(True)
        self.pricing_lbl.setStyleSheet(
            "background-color: rgba(10, 28, 44, 195); border: 1px solid #0397AB; "
            "border-radius: 2px; padding: 5px 10px; color: #0AC8B9; font-size: 11px;"
        )
        root.addWidget(self.pricing_lbl)

        # Dual-column body: Left = package & payment channel selector; Right = active QR card (keeps dialog height <=500px!)
        body_row = QHBoxLayout()
        body_row.setSpacing(14)

        left_col = QVBoxLayout()
        left_col.setSpacing(7)

        pkg_hdr = QLabel("1. 选择充值套餐（一瓶冰红茶畅玩十余局 · 永不过期）")
        pkg_hdr.setStyleSheet("color: #C8AA6E; font-weight: bold; font-size: 11px; background: transparent; border: none;")
        left_col.addWidget(pkg_hdr)

        self.pkg_container = QWidget()
        self.pkg_container.setStyleSheet("background: transparent; border: none;")
        self.pkg_lay = QVBoxLayout(self.pkg_container)
        self.pkg_lay.setContentsMargins(0, 0, 0, 0)
        self.pkg_lay.setSpacing(5)
        self._pkg_group = QButtonGroup(self)
        left_col.addWidget(self.pkg_container)

        ch_hdr = QLabel("2. 选择支付方式")
        ch_hdr.setStyleSheet("color: #C8AA6E; font-weight: bold; font-size: 11px; background: transparent; border: none;")
        left_col.addWidget(ch_hdr)

        self.channel_row = QHBoxLayout()
        self.channel_row.setSpacing(12)
        self._channel_group = QButtonGroup(self)
        self.wechat_radio = QRadioButton("微信扫码支付")
        self.alipay_radio = QRadioButton("支付宝扫码支付")
        self.wechat_radio.setChecked(True)
        self.wechat_radio.toggled.connect(lambda ok: ok and self._set_channel("manual_wechat"))
        self.alipay_radio.toggled.connect(lambda ok: ok and self._set_channel("manual_alipay"))
        self._channel_group.addButton(self.wechat_radio)
        self._channel_group.addButton(self.alipay_radio)
        self.channel_row.addWidget(self.wechat_radio)
        self.channel_row.addWidget(self.alipay_radio)
        self.channel_row.addStretch(1)
        left_col.addLayout(self.channel_row)

        create_row = QHBoxLayout()
        self.create_order_btn = HextechActionButton("生成支付二维码", variant="cyan", pointed_right=True, height=32)
        self.create_order_btn.clicked.connect(self._create_order_clicked)
        create_row.addWidget(self.create_order_btn)
        create_row.addStretch(1)
        left_col.addLayout(create_row)
        left_col.addStretch(1)
        body_row.addLayout(left_col, 1)

        # Active order QR card on the right side (shown once order is created)
        self.order_card = QFrame()
        self.order_card.setMinimumWidth(296)
        self.order_card.setStyleSheet(
            "QFrame { background-color: #091428; border: 1px solid #C8AA6E; border-radius: 2px; }"
        )
        ol = QVBoxLayout(self.order_card)
        ol.setContentsMargins(12, 9, 12, 9)
        ol.setSpacing(6)

        self.qr_image_lbl = QLabel("正在加载二维码...")
        self.qr_image_lbl.setFixedSize(176, 176)
        self.qr_image_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_image_lbl.setStyleSheet(
            "background-color: #FFFFFF; color: #091428; border: 2px solid #C8AA6E; border-radius: 2px; font-weight: bold;"
        )
        ol.addWidget(self.qr_image_lbl, alignment=Qt.AlignmentFlag.AlignCenter)

        self.order_info_lbl = QLabel("")
        self.order_info_lbl.setWordWrap(True)
        self.order_info_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.order_info_lbl.setStyleSheet("border: none; background: transparent; color: #FDE68A; font-size: 11px; font-weight: bold;")
        ol.addWidget(self.order_info_lbl)

        order_btn_row = QHBoxLayout()
        order_btn_row.setSpacing(6)
        self.copy_memo_btn = HextechActionButton("复制核对码", variant="gold", height=28)
        self.copy_memo_btn.clicked.connect(self._copy_memo)
        self.refresh_order_btn = HextechActionButton("我已支付，立即查单", variant="cyan", height=28)
        self.refresh_order_btn.clicked.connect(self._poll_tick)
        order_btn_row.addStretch(1)
        order_btn_row.addWidget(self.copy_memo_btn)
        order_btn_row.addWidget(self.refresh_order_btn)
        order_btn_row.addStretch(1)
        ol.addLayout(order_btn_row)

        # Paid ceremony panel (replaces QR immediately upon status == 'paid')
        self.paid_ceremony_box = QFrame()
        self.paid_ceremony_box.setStyleSheet(
            "QFrame { background-color: rgba(6, 32, 44, 232); border: 1px solid #0AC8B9; "
            "border-top: 2px solid #C8AA6E; border-radius: 2px; }"
        )
        pcl = QVBoxLayout(self.paid_ceremony_box)
        pcl.setContentsMargins(12, 14, 12, 14)
        pcl.setSpacing(8)
        self.paid_ceremony_title = QLabel("⚡ 海克斯核心充能完成")
        self.paid_ceremony_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.paid_ceremony_title.setFont(display_font(13, bold=True, letter_spacing=0.8))
        self.paid_ceremony_title.setStyleSheet("border: none; background: transparent; color: #0AC8B9;")
        self.paid_ceremony_detail = QLabel("")
        self.paid_ceremony_detail.setWordWrap(True)
        self.paid_ceremony_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.paid_ceremony_detail.setStyleSheet("border: none; background: transparent; color: #F0E6D2; font-size: 11px;")
        self.paid_ceremony_btn = HextechActionButton("⚡ 前往点击击碎封印", variant="cyan", pointed_right=True, height=34)
        self.paid_ceremony_btn.clicked.connect(self._on_paid_ceremony_clicked)
        pcl.addWidget(self.paid_ceremony_title)
        pcl.addWidget(self.paid_ceremony_detail)
        pcl.addWidget(self.paid_ceremony_btn)
        self.paid_ceremony_box.hide()
        ol.addWidget(self.paid_ceremony_box)

        self.order_card.hide()
        body_row.addWidget(self.order_card)
        root.addLayout(body_row)

        self.status_lbl = QLabel("按实际 Token 透明扣费 · 积分永不过期 · 余额归零永不锁本地 5ms 查表")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color: #A09B8C; font-size: 11px; background: transparent; border: none;")
        root.addWidget(self.status_lbl)

        foot = QHBoxLayout()
        foot.addStretch(1)
        self.close_btn = HextechActionButton("关闭", variant="gold", height=28)
        self.close_btn.clicked.connect(self.accept)
        foot.addWidget(self.close_btn)
        root.addLayout(foot)

        self._current_memo_code = ""
        if auto_load:
            self._load_packages_async()

    def set_balance(self, balance_credits: float | None, tier: str | None = None) -> None:
        """Update the match-friendly balance header pill (and optionally active tier)."""
        if tier is not None:
            self._tier = _normalize_tier(tier)
        if balance_credits is None:
            self._balance_credits = None
        else:
            try:
                self._balance_credits = max(0.0, float(balance_credits))
            except (TypeError, ValueError):
                self._balance_credits = 0.0
        self._refresh_balance_header()

    def _refresh_balance_header(self) -> None:
        if self._balance_credits is None:
            self.balance_lbl.setText("当前余额：待同步 · 积分永不过期")
        else:
            self.balance_lbl.setText(
                f"当前余额：{format_balance_match_friendly(self._balance_credits, self._tier)}"
            )

    @staticmethod
    def _resolve_package_numbers(pkg: dict[str, Any]) -> tuple[int, int, str]:
        raw_fen = pkg.get("amount_fen")
        raw_yuan = pkg.get("amount_yuan")
        if raw_fen is not None:
            amount_fen = int(raw_fen)
            yuan_str = f"¥{float(raw_yuan):.2f}" if raw_yuan is not None else f"¥{amount_fen / 100.0:.2f}"
        elif raw_yuan is not None:
            yuan_val = float(raw_yuan)
            amount_fen = int(round(yuan_val * 100))
            yuan_str = f"¥{yuan_val:.2f}"
        else:
            amount_fen = 0
            yuan_str = "¥0.00"

        raw_credits = pkg.get("credits")
        if raw_credits is None:
            raw_credits = pkg.get("credits_amount")
        if raw_credits is not None:
            credits_val = int(raw_credits)
        else:
            m = _LABEL_CREDITS_RE.search(str(pkg.get("label") or ""))
            if m:
                credits_val = int(m.group(1).replace(",", ""))
            else:
                credits_val = max(0, amount_fen * 10)
        return amount_fen, credits_val, yuan_str

    def _select_package(self, package_id: str) -> None:
        self._selected_package_id = package_id
        for pid, card in self._pkg_cards.items():
            card.set_selected(pid == package_id)

    def _set_channel(self, ch: str) -> None:
        self._selected_channel = ch

    def _set_status(self, text: str, color: str = "#A09B8C") -> None:
        self.status_lbl.setText(text)
        self.status_lbl.setStyleSheet(f"color: {color}; font-size: 11px; background: transparent; border: none;")

    def _populate_packages(self, data: dict[str, Any]) -> None:
        for btn in list(self._pkg_group.buttons()):
            self._pkg_group.removeButton(btn)
        self._pkg_cards.clear()
        while self.pkg_lay.count():
            it = self.pkg_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

        self._packages = list(data.get("packages") or [])
        self._channels = list(data.get("channels") or [])

        wallet_payload = data.get("wallet")
        if isinstance(wallet_payload, dict) and wallet_payload.get("balance_credits") is not None:
            self.set_balance(float(wallet_payload.get("balance_credits") or 0.0))

        ps = data.get("pricing_summary")
        if isinstance(ps, dict):
            try:
                a_in = ps.get("tier_a_input_credits_per_1k")
                a_out = ps.get("tier_a_output_credits_per_1k")
                b_in = ps.get("input_credits_per_1k")
                b_out = ps.get("output_credits_per_1k")
                if a_in is not None and a_out is not None and b_in is not None and b_out is not None:
                    self.pricing_lbl.setText(
                        f"智能混合约 3 毛/局 · A档 {a_in:g} / {a_out:g} · "
                        f"B档 {b_in:g} / {b_out:g} 积分/1K tokens  |  1,000 积分 = ¥1.00  |  永不过期"
                    )
            except Exception:
                pass

        enabled_chs = {str(ch.get("channel")) for ch in self._channels if ch.get("channel")}
        if enabled_chs:
            self.wechat_radio.setVisible("manual_wechat" in enabled_chs)
            self.wechat_radio.setEnabled("manual_wechat" in enabled_chs)
            self.alipay_radio.setVisible("manual_alipay" in enabled_chs)
            self.alipay_radio.setEnabled("manual_alipay" in enabled_chs)
            if "manual_wechat" in enabled_chs:
                self.wechat_radio.setChecked(True)
                self._selected_channel = "manual_wechat"
            elif "manual_alipay" in enabled_chs:
                self.alipay_radio.setChecked(True)
                self._selected_channel = "manual_alipay"
            self.create_order_btn.setEnabled(bool(self._packages))
        else:
            self._selected_channel = ""
            self.wechat_radio.setEnabled(False)
            self.alipay_radio.setEnabled(False)
            self.wechat_radio.setChecked(False)
            self.alipay_radio.setChecked(False)
            self.create_order_btn.setEnabled(False)

        avail_ids = {str(p.get("id") or "") for p in self._packages}
        if self._selected_package_id not in avail_ids:
            for preferred in ("pkg_99", "pkg_10", "pkg_5"):
                if preferred in avail_ids:
                    self._selected_package_id = preferred
                    break

        for idx, pkg in enumerate(self._packages):
            pid = str(pkg.get("id") or "")
            raw_label = str(pkg.get("label") or pid)
            clean_label = _LEGACY_MATCH_SUFFIX_RE.sub("", raw_label).strip() or pid
            amount_fen, credits_val, yuan_str = self._resolve_package_numbers(pkg)
            if credits_val > 0 and "积分" not in clean_label:
                radio_text = f"{yuan_str}  {clean_label} · {credits_val:,} 积分"
            else:
                radio_text = f"{yuan_str}  {clean_label}"
            rb = QRadioButton(radio_text)
            rb.setProperty("package_id", pid)
            is_selected = pid == self._selected_package_id or (
                idx == 0 and not any(p.get("id") == self._selected_package_id for p in self._packages)
            )
            if is_selected:
                rb.setChecked(True)
                self._selected_package_id = pid
            hint_txt = format_package_unit_hint(amount_fen, credits_val, tier=self._tier)
            badge_text = ""
            badge_gold = False
            if pid == "pkg_99" or (950 <= amount_fen <= 1050) or "主推" in clean_label:
                badge_text = "★ 主推"
                badge_gold = True
            elif pid == "pkg_5" or (450 <= amount_fen <= 550):
                badge_text = "尝鲜"
            card = _PackageOptionCard(
                rb,
                hint_txt,
                parent=self.pkg_container,
                badge_text=badge_text,
                badge_gold=badge_gold,
            )
            self._pkg_cards[pid] = card
            rb.toggled.connect(lambda checked, p_id=pid: checked and self._select_package(p_id))
            self._pkg_group.addButton(rb)
            self.pkg_lay.addWidget(card)

        if not enabled_chs:
            self._set_status("✕ 暂未开启任何在线收款渠道，请联系管理员后再试", "#F87171")
        elif not self._packages:
            self._set_status("✕ 暂无可选充值套餐", "#F87171")
        else:
            self._set_status("请选择套餐后点击「生成支付二维码」")
        self._sync_dialog_geometry()

    def _sync_dialog_geometry(self) -> None:
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()
        hint = self.sizeHint()
        if hint.isValid():
            self.resize(max(self.minimumWidth(), hint.width()), hint.height())

    def _on_packages_loaded(self, data: dict[str, Any]) -> None:
        if self._closed:
            return
        self._populate_packages(data)

    def _on_packages_error(self, msg: str) -> None:
        if self._closed:
            return
        self._set_status(f"✕ 加载套餐失败：{msg}", "#F87171")

    def _load_packages_async(self) -> None:
        self._set_status("⏳ 正在加载充值套餐...", "#FBBF24")

        def worker() -> None:
            try:
                data = self._client.get_packages()
                self._invoker.invoke.emit(lambda d=data: self._on_packages_loaded(d))
            except Exception as exc:
                self._invoker.invoke.emit(lambda m=str(exc): self._on_packages_error(m))

        threading.Thread(target=worker, name="HexLoadPackages", daemon=True).start()

    def _create_order_clicked(self) -> None:
        if not self._selected_channel:
            self._set_status("✕ 暂未开启任何在线收款渠道，请联系管理员", "#F87171")
            return
        if not self._selected_package_id:
            self._set_status("请先选择一个充值套餐", "#F87171")
            return
        self.create_order_btn.setEnabled(False)
        self._set_status("⏳ 正在生成专属收款订单...", "#FBBF24")
        pkg_id = self._selected_package_id
        ch = self._selected_channel

        def worker() -> None:
            try:
                order = self._client.create_order(pkg_id, ch)
                qr_bytes: bytes | None = None
                qr_url = str(order.get("qr_image_url") or "").strip()
                if qr_url:
                    try:
                        qr_bytes = self._client.fetch_qr_image_bytes(qr_url)
                    except Exception:
                        qr_bytes = None
                self._invoker.invoke.emit(lambda o=order, q=qr_bytes: self._on_order_created(o, q))
            except CloudCoachError as exc:
                self._invoker.invoke.emit(lambda m=exc.message: self._on_order_error(m))
            except Exception as exc:
                self._invoker.invoke.emit(lambda m=str(exc): self._on_order_error(m))

        threading.Thread(target=worker, name="HexCreateOrder", daemon=True).start()

    def _on_order_created(self, order: dict[str, Any], qr_bytes: bytes | None) -> None:
        if self._closed:
            return
        self.create_order_btn.setEnabled(True)
        self.show_created_order(order, qr_bytes)
        self._poll_timer.start()

    def _on_order_error(self, msg: str) -> None:
        if self._closed:
            return
        self.create_order_btn.setEnabled(True)
        self._set_status(f"✕ 创建订单失败：{msg}", "#F87171")

    def show_created_order(self, order: dict[str, Any], qr_bytes: bytes | None = None) -> None:
        """Display created order QR image, exact cent-offset amount, and 4-char memo code."""
        self._current_order_no = str(order.get("order_no") or "")
        self._current_memo_code = str(order.get("memo_code") or "")
        self._paid_notified = False
        self._poll_in_flight = False

        pm: QPixmap | None = None
        if qr_bytes:
            cand = QPixmap()
            if cand.loadFromData(qr_bytes):
                pm = cand.scaled(168, 168, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        if pm is None and order.get("qr_payload"):
            pm = _render_qr_payload_pixmap(str(order.get("qr_payload")), size=168)

        if pm is not None and not pm.isNull():
            self.qr_image_lbl.setPixmap(pm)
            self.qr_image_lbl.setText("")
        else:
            self.qr_image_lbl.setPixmap(QPixmap())
            self.qr_image_lbl.setText(
                f"请按下方金额付款\n¥{order.get('pay_amount_yuan', '')}\n核对码：{self._current_memo_code}"
            )

        yuan = order.get("pay_amount_yuan") or f"{int(order.get('pay_amount_fen', 0)) / 100:.2f}"
        ch_lbl = order.get("channel_label") or order.get("channel") or "扫码支付"
        credits_amt = int(order.get("credits_amount") or 0)
        est_added = estimate_remaining_matches(credits_amt, self._tier)
        self.order_info_lbl.setText(
            f"{ch_lbl} · 请精确支付 ¥{yuan}（切勿改价）\n"
            f"备注核对码：{self._current_memo_code}  |  +{credits_amt:,} 积分（约 {est_added} 局）\n"
            f"单号：{self._current_order_no}（15 分钟内有效）"
        )
        self.paid_ceremony_box.hide()
        self.qr_image_lbl.show()
        self.order_info_lbl.show()
        self.copy_memo_btn.show()
        self.refresh_order_btn.show()
        self.order_card.show()
        self._sync_dialog_geometry()
        self._set_status("⏳ 等待扫码支付确认中（每 3 秒自动刷新订单状态）...", "#0AC8B9")

    def _on_paid_ceremony_clicked(self) -> None:
        self._auto_close_timer.stop()
        self.unseal_requested = True
        self.accept()

    def _copy_memo(self) -> None:
        if self._current_memo_code:
            cb = QApplication.clipboard()
            if cb is not None:
                cb.setText(self._current_memo_code)
            self._set_status(f"✓ 已复制核对码：{self._current_memo_code}", "#0AC8B9")

    def _handle_poll_result(self, order_data: dict[str, Any]) -> None:
        self._poll_in_flight = False
        if self._closed or not isinstance(order_data, dict):
            return
        resp_order_no = str(order_data.get("order_no") or "").strip()
        if not resp_order_no or resp_order_no != self._current_order_no:
            return
        st = str(order_data.get("status") or "")
        if st == "paid":
            self._poll_timer.stop()
            wallet = order_data.get("wallet") if isinstance(order_data.get("wallet"), dict) else {}
            bal = float(wallet.get("balance_credits") or 0.0)
            added = int(order_data.get("credits_amount") or 0)
            self.set_balance(bal)
            # Replace QR with immediate Hextech completion ceremony (Track A lobby vs Track B in-game)
            self.qr_image_lbl.hide()
            self.order_info_lbl.hide()
            self.copy_memo_btn.hide()
            self.refresh_order_btn.hide()
            friendly = format_balance_match_friendly(bal, self._tier)
            if self._in_game:
                self.paid_ceremony_title.setText("✓ 核心充能成功 · 正在重发本局推演")
                self.paid_ceremony_detail.setText(
                    f"已到账 +{added:,} 积分（当前{friendly}）\n"
                    "局内抽屉将自动破封并更新当前局势，即将返回战场…"
                )
                self.paid_ceremony_btn.setText("⚔ 立即切回游戏")
                self._auto_close_timer.start(1600)
            else:
                self.paid_ceremony_title.setText("⚡ 海克斯核心充能完成")
                self.paid_ceremony_detail.setText(
                    f"已到账 +{added:,} 积分（当前{friendly}）\n"
                    + (
                        "点击下方按钮前往主控台，亲手击碎枯朽封印唤醒 AI 参谋！"
                        if self._is_first_recharge
                        else "AI 参谋核心已满载就绪，随时开启下一局 10 人实时推演。"
                    )
                )
                self.paid_ceremony_btn.setText(
                    "⚡ 前往点击击碎封印" if self._is_first_recharge else "✓ 立即启用 AI 参谋"
                )
            self.paid_ceremony_box.show()
            self.order_card.show()
            self._set_status(
                f"✓ 充值成功！已到账 +{added:,} 积分，当前{friendly} · AI 参谋已恢复使用",
                "#09C269",
            )
            if not self._paid_notified:
                self._paid_notified = True
                if self._on_paid:
                    self._on_paid(order_data)
        elif st in ("expired", "cancelled"):
            self._poll_timer.stop()
            self._set_status("✕ 订单已过期或取消，请重新生成二维码", "#F87171")

    def poll_once_sync(self) -> dict[str, Any] | None:
        """Synchronously poll current order once (used by deterministic tests and manual check)."""
        if not self._current_order_no:
            return None
        data = self._client.poll_order(self._current_order_no)
        self._handle_poll_result(data)
        return data

    def _poll_tick(self) -> None:
        if self._closed or not self._current_order_no or self._poll_in_flight:
            return
        self._poll_in_flight = True
        order_no = self._current_order_no

        def worker() -> None:
            try:
                data = self._client.poll_order(order_no)
                self._invoker.invoke.emit(lambda d=data: self._handle_poll_result(d))
            except Exception:
                self._invoker.invoke.emit(lambda: setattr(self, "_poll_in_flight", False))

        threading.Thread(target=worker, name="HexPollOrder", daemon=True).start()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closed = True
        self._poll_timer.stop()
        self._auto_close_timer.stop()
        super().closeEvent(event)

    def reject(self) -> None:
        self._closed = True
        self._poll_timer.stop()
        self._auto_close_timer.stop()
        super().reject()

    def accept(self) -> None:
        self._closed = True
        self._poll_timer.stop()
        self._auto_close_timer.stop()
        super().accept()


def _format_byte_size(num_bytes: int) -> str:
    if num_bytes <= 0:
        return "未知大小"
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes} B"


class UpdatePromptDialog(QDialog):
    """Hextech dark client self-update prompt with streaming progress & SHA-256 verification."""

    def __init__(
        self,
        info: UpdateInfo,
        cloud_base_url: str,
        dest_dir: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._info = info
        self._cloud_base_url = (cloud_base_url or "").strip().rstrip("/")
        self._dest_dir = Path(dest_dir)
        self._invoker = UiBridgeInvoker()
        self._busy = False
        self._closed = False
        self.downloaded_artifact_path: Path | None = None
        self.force_exit_requested: bool = False

        self.setWindowTitle("HexHelper · 版本更新")
        self.setWindowIcon(load_app_icon())
        self.setModal(True)
        if info.force_update:
            self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.setMinimumWidth(460)
        self.setStyleSheet(
            BASE_OVERLAY_QSS
            + """
            QDialog {
                background-color: #040E1A;
                border: 1px solid #C8AA6E;
            }
            QProgressBar {
                background-color: #091428;
                border: 1px solid #785A28;
                border-radius: 2px;
                text-align: center;
                color: #F0E6D2;
                font-size: 11px;
                font-weight: bold;
                min-height: 18px;
            }
            QProgressBar::chunk {
                background-color: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #0397AB, stop:1 #0AC8B9
                );
            }
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(10)

        hdr = QLabel("HEXHELPER 客户端版本更新")
        hdr.setFont(display_font(14, bold=True, letter_spacing=1.2))
        hdr.setStyleSheet("color: #F0E6D2; background: transparent; border: none;")
        root.addWidget(hdr)

        size_str = _format_byte_size(info.file_size)
        force_tag = "  ·  【强制更新】" if info.force_update else ""
        self.version_lbl = QLabel(
            f"版本升级：v{info.current_version}  →  v{info.latest_version}（{size_str}{force_tag}）"
        )
        self.version_lbl.setWordWrap(True)
        self.version_lbl.setStyleSheet(
            "background-color: rgba(10, 28, 44, 195); border: 1px solid #0397AB; "
            "border-radius: 2px; padding: 7px 10px; color: #0AC8B9; font-size: 12px; font-weight: bold;"
        )
        root.addWidget(self.version_lbl)

        changelog_card = QFrame()
        changelog_card.setStyleSheet(
            "QFrame { background-color: #091428; border: 1px solid #463714; border-radius: 2px; }"
        )
        cl_lay = QVBoxLayout(changelog_card)
        cl_lay.setContentsMargins(12, 10, 12, 10)
        cl_lay.setSpacing(6)
        cl_title = QLabel("更新内容说明：")
        cl_title.setStyleSheet("color: #C8AA6E; font-weight: bold; font-size: 11px; border: none; background: transparent;")
        self.changelog_lbl = QLabel((info.changelog or "").strip() or "包含性能优化、对局数据更新与稳定性提升。")
        self.changelog_lbl.setWordWrap(True)
        self.changelog_lbl.setStyleSheet("color: #F0E6D2; font-size: 11px; border: none; background: transparent;")
        cl_lay.addWidget(cl_title)
        cl_lay.addWidget(self.changelog_lbl)
        root.addWidget(changelog_card)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.hide()
        root.addWidget(self.progress_bar)

        sha_preview = f"{info.sha256[:16]}..." if info.sha256 else "未提供"
        init_status = (
            f"当前版本已不受支持，请立即更新至 v{info.latest_version}（SHA-256: {sha_preview}）"
            if info.force_update
            else f"SHA-256 校验值：{sha_preview}（下载后将自动校验文件完整性）"
        )
        self.status_lbl = QLabel(init_status)
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color: #A09B8C; font-size: 11px; background: transparent; border: none;")
        root.addWidget(self.status_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self.later_btn = HextechActionButton("稍后提醒", variant="gold", height=34)
        self.later_btn.clicked.connect(self.reject)
        self.exit_app_btn = HextechActionButton("退出程序", variant="gold", height=34)
        self.exit_app_btn.clicked.connect(self._on_force_exit_clicked)
        self.download_btn = HextechActionButton("立即下载并更新", variant="cyan", pointed_right=True, height=34)
        self.download_btn.clicked.connect(self._start_download)

        if info.force_update:
            self.later_btn.setEnabled(False)
            self.later_btn.hide()
            self.exit_app_btn.setEnabled(True)
            self.exit_app_btn.show()
        else:
            self.exit_app_btn.setEnabled(False)
            self.exit_app_btn.hide()

        btn_row.addWidget(self.later_btn)
        btn_row.addWidget(self.exit_app_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.download_btn)
        root.addLayout(btn_row)

    def _set_status(self, text: str, *, color: str = "#A09B8C") -> None:
        self.status_lbl.setText(text)
        self.status_lbl.setStyleSheet(f"color: {color}; font-size: 11px; background: transparent; border: none;")

    def _start_download(self) -> None:
        if self._busy or self._closed:
            return
        self._busy = True
        self.download_btn.setEnabled(False)
        if not self._info.force_update:
            self.later_btn.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self._set_status("⏳ 正在下载更新包并实时计算 SHA-256...", color="#FBBF24")

        def on_progress(downloaded: int, total: int) -> None:
            if self._closed:
                return
            self._invoker.invoke.emit(lambda d=downloaded, t=total: self._on_progress_ui(d, t))

        def worker() -> None:
            try:
                path = download_and_verify_update(
                    self._info,
                    self._dest_dir,
                    base_url=self._cloud_base_url,
                    progress_cb=on_progress,
                )
                self._invoker.invoke.emit(lambda p=path: self._on_download_ok(p))
            except UpdateVerificationError as exc:
                self._invoker.invoke.emit(lambda m=str(exc): self._on_download_fail(m))
            except Exception as exc:
                self._invoker.invoke.emit(lambda m=str(exc): self._on_download_fail(m))

        threading.Thread(target=worker, name="HexUpdateDownload", daemon=True).start()

    def _on_progress_ui(self, downloaded: int, total: int) -> None:
        if self._closed:
            return
        if total > 0:
            pct = max(0, min(100, int((downloaded * 100) / total)))
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(pct)
            self._set_status(
                f"⏳ 正在下载更新包 ({_format_byte_size(downloaded)} / {_format_byte_size(total)})...",
                color="#0AC8B9",
            )
        else:
            self.progress_bar.setRange(0, 0)
            self._set_status(f"⏳ 正在下载更新包 ({_format_byte_size(downloaded)})...", color="#0AC8B9")

    def _on_download_ok(self, artifact_path: Path) -> None:
        self._busy = False
        if self._closed:
            return
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.downloaded_artifact_path = artifact_path
        self._set_status("✓ SHA-256 完整性校验通过，正在准备安装更新...", color="#09C269")
        self.accept()

    def _on_download_fail(self, msg: str) -> None:
        self._busy = False
        if self._closed:
            return
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.download_btn.setEnabled(True)
        if not self._info.force_update:
            self.later_btn.setEnabled(True)
        self._set_status(f"✕ 更新失败：{msg}", color="#F87171")

    def _on_force_exit_clicked(self) -> None:
        self.force_exit_requested = True
        self._closed = True
        super().reject()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._info.force_update and event.key() == Qt.Key.Key_Escape:
            event.ignore()
            return
        super().keyPressEvent(event)

    def reject(self) -> None:
        if self._info.force_update and not self.force_exit_requested:
            return
        self._closed = True
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if (
            self._info.force_update
            and not self.force_exit_requested
            and self.result() != int(QDialog.DialogCode.Accepted)
        ):
            event.ignore()
            return
        self._closed = True
        super().closeEvent(event)
