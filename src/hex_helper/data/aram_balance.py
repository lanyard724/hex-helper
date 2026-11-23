"""Official ARAM champion hidden balance modifiers & Hex Mayhem ceiling tags (0ms offline lookup)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class AramBalanceMod:
    hero_id: str
    dmg_dealt: float = 1.0
    dmg_taken: float = 1.0
    healing: float = 1.0
    shielding: float = 1.0
    ability_haste: int = 0
    tenacity: float = 1.0
    total_as: float = 1.0
    energyregen_mod: float = 1.0

    def format_badge(self, max_tokens: int = 3) -> tuple[str, str]:
        """Return `(compact_text, sentiment)` where sentiment is buff | nerf | mixed | neutral."""
        parts: list[str] = []
        buff_score = 0.0
        nerf_score = 0.0

        def _pct(val: float) -> str:
            delta = int(round((val - 1.0) * 100))
            return f"{delta:+d}%"

        if abs(self.dmg_dealt - 1.0) > 1e-4:
            parts.append(f"伤{_pct(self.dmg_dealt)}")
            if self.dmg_dealt > 1.0:
                buff_score += (self.dmg_dealt - 1.0) * 100
            else:
                nerf_score += (1.0 - self.dmg_dealt) * 100
        if abs(self.dmg_taken - 1.0) > 1e-4:
            parts.append(f"承{_pct(self.dmg_taken)}")
            if self.dmg_taken < 1.0:
                buff_score += (1.0 - self.dmg_taken) * 100
            else:
                nerf_score += (self.dmg_taken - 1.0) * 100
        if self.ability_haste != 0:
            parts.append(f"极{self.ability_haste:+d}")
            if self.ability_haste > 0:
                buff_score += self.ability_haste * 0.4
            else:
                nerf_score += abs(self.ability_haste) * 0.4
        if abs(self.healing - 1.0) > 1e-4:
            parts.append(f"治{_pct(self.healing)}")
            if self.healing > 1.0:
                buff_score += (self.healing - 1.0) * 60
            else:
                nerf_score += (1.0 - self.healing) * 60
        if abs(self.shielding - 1.0) > 1e-4:
            parts.append(f"盾{_pct(self.shielding)}")
            if self.shielding > 1.0:
                buff_score += (self.shielding - 1.0) * 60
            else:
                nerf_score += (1.0 - self.shielding) * 60
        if abs(self.tenacity - 1.0) > 1e-4:
            parts.append(f"韧{_pct(self.tenacity)}")
            if self.tenacity > 1.0:
                buff_score += (self.tenacity - 1.0) * 40
            else:
                nerf_score += (1.0 - self.tenacity) * 40
        if abs(self.total_as - 1.0) > 1e-4:
            parts.append(f"攻速{_pct(self.total_as)}")
            if self.total_as > 1.0:
                buff_score += (self.total_as - 1.0) * 100
            else:
                nerf_score += (1.0 - self.total_as) * 100

        if not parts:
            return "", "neutral"
        if buff_score > 0 and nerf_score == 0:
            sentiment = "buff"
        elif nerf_score > 0 and buff_score == 0:
            sentiment = "nerf"
        elif buff_score >= nerf_score * 1.5:
            sentiment = "buff"
        elif nerf_score >= buff_score * 1.5:
            sentiment = "nerf"
        else:
            sentiment = "mixed"
        return " ".join(parts[:max_tokens]), sentiment


# Official Riot ARAM balance modifiers (source: League Wiki Module:ChampionData/data, 134 non-default champions)
_ARAM_BALANCE_RAW: dict[str, dict[str, float | int]] = {
    '1': {'dmg_dealt': 0.95, 'dmg_taken': 1.05, 'shielding': 0.9},
    '2': {'dmg_dealt': 1.05},
    '3': {'dmg_dealt': 0.9, 'dmg_taken': 1.05},
    '5': {'dmg_dealt': 0.95},
    '6': {'dmg_taken': 1.05},
    '7': {'dmg_dealt': 1.1, 'dmg_taken': 0.9, 'ability_haste': 20},
    '9': {'dmg_dealt': 0.95, 'dmg_taken': 1.05},
    '10': {'dmg_dealt': 0.95, 'dmg_taken': 1.1},
    '12': {'dmg_dealt': 0.95, 'dmg_taken': 1.05, 'healing': 0.8},
    '13': {'dmg_dealt': 1.05, 'dmg_taken': 0.95},
    '14': {'dmg_dealt': 0.9, 'dmg_taken': 1.1, 'ability_haste': -10},
    '15': {'dmg_dealt': 0.93, 'dmg_taken': 1.05},
    '16': {'ability_haste': 10},
    '17': {'dmg_dealt': 0.95, 'dmg_taken': 1.1, 'ability_haste': -15},
    '18': {'dmg_dealt': 1.05, 'dmg_taken': 0.95},
    '19': {'dmg_dealt': 1.05, 'dmg_taken': 0.95},
    '20': {'dmg_dealt': 1.1, 'dmg_taken': 0.9, 'healing': 1.1, 'shielding': 1.2, 'tenacity': 1.2},
    '21': {'dmg_dealt': 0.95, 'dmg_taken': 1.05},
    '22': {'dmg_dealt': 1.05},
    '23': {'dmg_dealt': 1.1, 'dmg_taken': 0.9, 'healing': 1.2},
    '24': {'dmg_dealt': 1.05, 'dmg_taken': 0.97},
    '25': {'dmg_dealt': 0.95, 'dmg_taken': 1.1},
    '26': {'dmg_dealt': 1.05, 'dmg_taken': 0.95},
    '27': {'dmg_taken': 1.08},
    '28': {'dmg_dealt': 1.1, 'dmg_taken': 0.9, 'tenacity': 1.2},
    '29': {'dmg_taken': 0.95},
    '30': {'dmg_dealt': 0.93, 'dmg_taken': 1.05},
    '31': {'dmg_taken': 1.1},
    '34': {'dmg_dealt': 1.05},
    '35': {'dmg_dealt': 1.05},
    '36': {'dmg_dealt': 0.9, 'dmg_taken': 1.05, 'healing': 0.9},
    '38': {'dmg_taken': 0.95},
    '39': {'dmg_dealt': 1.05, 'ability_haste': 20},
    '40': {'dmg_dealt': 0.95, 'dmg_taken': 1.05, 'healing': 0.9},
    '41': {'dmg_dealt': 1.05},
    '42': {'dmg_taken': 0.9, 'ability_haste': -20},
    '44': {'dmg_taken': 1.1},
    '45': {'dmg_dealt': 0.93, 'dmg_taken': 1.1},
    '48': {'dmg_taken': 1.05},
    '50': {'dmg_dealt': 0.95, 'dmg_taken': 1.15, 'healing': 0.8},
    '55': {'tenacity': 1.2, 'ability_haste': 10},
    '56': {'dmg_dealt': 1.1, 'dmg_taken': 0.9, 'healing': 1.2},
    '57': {'dmg_dealt': 0.85, 'dmg_taken': 1.1},
    '58': {'healing': 1.1},
    '60': {'dmg_dealt': 1.05, 'dmg_taken': 0.9, 'tenacity': 1.1},
    '63': {'dmg_dealt': 0.95, 'dmg_taken': 1.1, 'ability_haste': -10},
    '64': {'dmg_dealt': 1.05, 'dmg_taken': 0.95, 'healing': 1.1, 'shielding': 1.2, 'energyregen_mod': 1.2},
    '67': {'dmg_taken': 0.95},
    '74': {'dmg_dealt': 0.9, 'dmg_taken': 1.1},
    '75': {'dmg_dealt': 0.9, 'dmg_taken': 1.05},
    '76': {'dmg_dealt': 1.1},
    '79': {'dmg_taken': 0.95},
    '84': {'dmg_taken': 0.95, 'tenacity': 1.2, 'energyregen_mod': 1.2},
    '85': {'energyregen_mod': 1.2},
    '86': {'dmg_taken': 0.95},
    '89': {'dmg_dealt': 0.95, 'dmg_taken': 1.05},
    '90': {'dmg_dealt': 0.9, 'dmg_taken': 1.1},
    '91': {'dmg_dealt': 1.05, 'dmg_taken': 0.95, 'tenacity': 1.2},
    '92': {'dmg_dealt': 1.05, 'dmg_taken': 0.92},
    '96': {'dmg_dealt': 0.92, 'dmg_taken': 1.1},
    '98': {'dmg_dealt': 0.95, 'dmg_taken': 1.05},
    '99': {'dmg_dealt': 0.9, 'dmg_taken': 1.1, 'shielding': 0.9},
    '101': {'dmg_dealt': 0.93, 'dmg_taken': 1.05},
    '102': {'dmg_taken': 0.95},
    '103': {'healing': 0.9},
    '105': {'dmg_dealt': 1.05, 'dmg_taken': 0.95, 'tenacity': 1.1},
    '107': {'dmg_dealt': 1.05, 'dmg_taken': 0.92, 'tenacity': 1.2},
    '111': {'dmg_dealt': 0.95, 'dmg_taken': 1.1},
    '114': {'dmg_taken': 0.95},
    '115': {'dmg_dealt': 0.92, 'dmg_taken': 1.2, 'ability_haste': -20},
    '119': {'dmg_dealt': 1.05, 'dmg_taken': 0.95},
    '120': {'dmg_dealt': 1.05, 'dmg_taken': 0.9, 'healing': 1.2, 'ability_haste': 10},
    '121': {'dmg_dealt': 1.1, 'dmg_taken': 0.9, 'healing': 1.2, 'tenacity': 1.2},
    '122': {'dmg_dealt': 1.05},
    '126': {'dmg_dealt': 1.05},
    '127': {'dmg_dealt': 0.95, 'dmg_taken': 1.05},
    '133': {'dmg_dealt': 1.1, 'dmg_taken': 0.9, 'tenacity': 1.2, 'total_as': 1.025},
    '134': {'dmg_dealt': 1.05, 'ability_haste': 5},
    '141': {'healing': 0.8, 'tenacity': 1.2},
    '142': {'dmg_dealt': 1.1, 'dmg_taken': 0.95},
    '143': {'dmg_dealt': 0.9, 'dmg_taken': 1.05, 'ability_haste': -10},
    '145': {'dmg_taken': 0.9, 'total_as': 1.025},
    '147': {'dmg_taken': 1.2, 'healing': 0.8, 'shielding': 0.8, 'ability_haste': -20},
    '150': {'dmg_dealt': 1.05, 'dmg_taken': 0.95, 'total_as': 1.025},
    '154': {'dmg_taken': 0.96, 'healing': 1.1},
    '157': {'total_as': 1.025},
    '161': {'dmg_dealt': 0.95, 'dmg_taken': 1.05},
    '164': {'dmg_dealt': 1.05, 'dmg_taken': 0.95, 'healing': 1.2, 'shielding': 1.1, 'ability_haste': 10},
    '166': {'dmg_dealt': 1.05, 'dmg_taken': 0.95},
    '202': {'dmg_dealt': 0.95, 'dmg_taken': 1.05},
    '203': {'dmg_dealt': 1.1, 'dmg_taken': 0.9, 'total_as': 1.025},
    '221': {'dmg_dealt': 1.1, 'dmg_taken': 0.95},
    '222': {'dmg_dealt': 0.9, 'dmg_taken': 1.05},
    '223': {'dmg_taken': 1.05},
    '233': {'dmg_dealt': 1.05, 'healing': 1.1},
    '234': {'dmg_dealt': 1.05},
    '235': {'dmg_dealt': 0.92, 'dmg_taken': 1.05},
    '236': {'dmg_dealt': 1.05, 'dmg_taken': 0.95, 'tenacity': 1.2, 'ability_haste': 10},
    '238': {'dmg_dealt': 1.05, 'dmg_taken': 0.95, 'tenacity': 1.2, 'energyregen_mod': 1.2},
    '240': {'dmg_dealt': 1.05, 'dmg_taken': 0.9},
    '245': {'dmg_dealt': 1.1, 'tenacity': 1.2},
    '246': {'dmg_dealt': 1.15, 'dmg_taken': 0.9, 'tenacity': 1.2, 'ability_haste': 20},
    '254': {'dmg_dealt': 1.05, 'dmg_taken': 0.95},
    '266': {'dmg_dealt': 1.05},
    '267': {'dmg_dealt': 0.95, 'dmg_taken': 1.05},
    '268': {'dmg_taken': 0.95, 'total_as': 1.025, 'ability_haste': 20},
    '412': {'dmg_taken': 1.05},
    '420': {'dmg_dealt': 0.95, 'dmg_taken': 1.05, 'healing': 0.8},
    '421': {'dmg_dealt': 1.05, 'dmg_taken': 0.9},
    '427': {'dmg_dealt': 0.95, 'shielding': 0.9},
    '429': {'dmg_dealt': 1.05},
    '432': {'dmg_dealt': 1.15, 'dmg_taken': 0.85, 'healing': 1.2},
    '497': {'dmg_taken': 0.95, 'ability_haste': 10},
    '498': {'dmg_dealt': 1.05, 'total_as': 1.03},
    '516': {'dmg_dealt': 0.9, 'dmg_taken': 1.05},
    '517': {'dmg_dealt': 1.05},
    '526': {'dmg_dealt': 0.95, 'shielding': 0.9},
    '555': {'dmg_dealt': 1.1, 'dmg_taken': 0.9, 'tenacity': 1.2},
    '711': {'dmg_dealt': 0.95},
    '777': {'dmg_dealt': 1.05, 'dmg_taken': 0.97, 'total_as': 1.025},
    '799': {'dmg_taken': 1.05},
    '800': {'ability_haste': -10},
    '805': {'dmg_dealt': 1.15},
    '875': {'dmg_taken': 1.1},
    '876': {'dmg_dealt': 0.95, 'dmg_taken': 1.05},
    '887': {'dmg_dealt': 1.02},
    '888': {'dmg_dealt': 0.95, 'dmg_taken': 1.05},
    '893': {'ability_haste': 10},
    '897': {'dmg_dealt': 1.05},
    '901': {'dmg_taken': 1.02, 'ability_haste': -10},
    '902': {'healing': 0.95, 'shielding': 0.9, 'ability_haste': -10},
    '904': {'dmg_dealt': 0.95, 'dmg_taken': 1.05},
    '910': {'dmg_dealt': 0.95},
    '950': {'dmg_dealt': 1.1, 'ability_haste': 10},
}


@lru_cache(maxsize=256)
def get_aram_balance(hero_id: str | int) -> AramBalanceMod | None:
    hid = str(hero_id).strip()
    raw = _ARAM_BALANCE_RAW.get(hid)
    if not raw:
        return None
    return AramBalanceMod(
        hero_id=hid,
        dmg_dealt=float(raw.get("dmg_dealt", 1.0)),
        dmg_taken=float(raw.get("dmg_taken", 1.0)),
        healing=float(raw.get("healing", 1.0)),
        shielding=float(raw.get("shielding", 1.0)),
        ability_haste=int(raw.get("ability_haste", 0)),
        tenacity=float(raw.get("tenacity", 1.0)),
        total_as=float(raw.get("total_as", 1.0)),
        energyregen_mod=float(raw.get("energyregen_mod", 1.0)),
    )


@lru_cache(maxsize=256)
def get_hero_ceiling_tag(hero_id: str | int) -> str:
    """Return a crisp 4-6 char Hex Mayhem ceiling tag for champions featured in curated high-ceiling combos."""
    hid = str(hero_id).strip()
    if not hid:
        return ""
    try:
        from hex_helper.scoring.augment_combo import get_augment_combo_db

        db = get_augment_combo_db()
    except Exception:
        return ""
    matches = [r for r in db.entries if hid in r.hero_bonus]
    if not matches:
        return ""
    # Prefer signature gamble ceiling combos first, then shortest/cleanest combo name
    matches.sort(key=lambda r: (0 if r.gamble else 1, len(r.name)))
    top = matches[0]
    name = top.name.replace(" ", "")
    return name[:8] if len(name) > 8 else name
