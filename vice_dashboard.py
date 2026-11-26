"""Vice Dashboard - monitoring système + lecteur audio dans le terminal, ambiance coucher de soleil néon.

Installer :
    pip install -r requirements.txt

Lancer :  python vice_dashboard.py [dossier_musique] [--titre "MON NOM"] [--sous-titre "texte"]
Touches : entrée = lire | espace = pause | n / p = suivant / précédent
          ← → = -5s / +5s | + / - = volume | f = favori | s = aléatoire
          r = répéter | 1 / 2 = afficher/masquer cpu / mem | q = quitter
Musique : dossier "Music" de l'utilisateur par défaut (mp3, flac, wav, ogg, m4a).
"""
import io
import json
import os
import random
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import psutil
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402

try:
    import mutagen
except Exception:  # pragma: no cover
    mutagen = None
try:
    import soundfile as sf
except Exception:  # pragma: no cover
    sf = None
try:
    from PIL import Image as PILImage
except Exception:  # pragma: no cover
    PILImage = None
try:
    from textual_image.renderable import Image as _RenduAuto
    from textual_image.renderable.unicode import Image as _RenduUnicode
    from textual_image.widget import HalfcellImage
    from textual_image.widget import Image as CoverImage
    if _RenduAuto is _RenduUnicode:  # terminal sans sixel/kitty : demi-blocs en couleur plutôt que caractères gris
        CoverImage = HalfcellImage
except Exception:  # pragma: no cover
    CoverImage = None
try:
    import win32pdh
except Exception:  # pragma: no cover
    win32pdh = None

# thème Vice : coucher de soleil néon
FOND = "#0a0614"
CADRE = "#3b2a5c"
CADRE_SYS = "#1aa6c9"
VIOLET = "#c56cf0"
ROSE = "#ff2e88"
ORANGE = "#ff8a3d"
BLEU = "#00e5ff"
GRIS = "#9a8fb8"
JAUNE = "#ffd23f"
ROUGE = "#ff3b3b"
COUCHANT = ["#ffe066", "#ffb03a", "#ff8a3d", "#ff4f6d", "#ff2e88", "#c03aa8"]  # haut -> bas du soleil
GRAPHE = [ROSE, "#c86ad0", "#6ea0f0", BLEU]  # haut -> bas des graphes
DEGRADE = ["#b14aed", "#d23fc8", "#ff2e88", "#ff4f6d", "#ff6b4a", "#ff8a3d", "#ffb03a", "#ffd23f"]  # spectre haut -> bas
EXTS = {".mp3", ".flac", ".wav", ".ogg", ".m4a"}
FAVORIS = Path.home() / ".vice_dashboard_favoris.json"

# points braille remplis depuis le bas : colonne gauche / colonne droite
BR_G = [0, 0x40, 0x44, 0x46, 0x47]
BR_D = [0, 0x80, 0xA0, 0xB0, 0xB8]


def degrade(couleurs, t: float) -> str:
    """Couleur hex à la position t (0..1) d'un dégradé multi-couleurs."""
    t = max(0.0, min(1.0, t)) * (len(couleurs) - 1)
    i = min(int(t), len(couleurs) - 2)
    a, b, f = couleurs[i], couleurs[i + 1], t - i
    return "#" + "".join(f"{round(int(a[k:k + 2], 16) * (1 - f) + int(b[k:k + 2], 16) * f):02x}" for k in (1, 3, 5))


def barre(pct: float, largeur: int = 20, couleurs=(BLEU, ROSE)) -> Text:
    n = int(max(0, min(100, pct)) / 100 * largeur)
    t = Text()
    for i in range(n):
        t.append("■", style=degrade(couleurs, i / max(1, largeur - 1)))
    t.append("▫" * (largeur - n), style=CADRE)
    return t


PALMIER = [  # silhouette en pixels (demi-caractères)
    "..####.####..",
    ".##...###..##",
    "#....#.#.#..#",
    ".....##......",
    "......#......",
    ".....##......",
    ".....#.......",
    "....##.......",
]


def banniere(larg: int, phase: int, titre: str = "VICE DASHBOARD", sous_titre: str = "sunset terminal") -> Text:
    """Soleil couchant à bandes, palmiers, mer scintillante + nom en néon (5 lignes)."""
    H, horizon = 10, 8  # pixels de haut, ligne de l'horizon
    cx, R = larg // 2, 9
    pix = [[None] * larg for _ in range(H)]
    for y in range(H):
        for x in range(larg):
            if y < horizon:
                c = degrade(["#0a0614", "#1e0b35", "#4a1450"], y / (horizon - 1))
                dx, dy = x - cx, (y - horizon) * 1.0
                if dx * dx + dy * dy <= R * R and not (y >= 3 and (y + phase // 2) % 3 == 0):
                    c = degrade(COUCHANT, y / (horizon - 1))
            else:  # mer : reflet du soleil en traits qui ondulent
                c = "#2a0f45" if y == horizon else "#140a2a"
                demi = R - (y - horizon) * 4
                if abs(x - cx) < demi and (x + (phase if y == horizon else -phase)) % 6 < 4:
                    c = ROSE if y == horizon else ORANGE
            pix[y][x] = c
    for px in (2, 12, larg - 15):  # palmiers
        for y, rang in enumerate(PALMIER):
            for x, ch in enumerate(rang):
                if ch == "#" and 0 <= px + x < larg:
                    pix[y][px + x] = "#05020a"
    textes = {1: (titre, f"bold {ROSE}"), 2: (sous_titre, f"italic {BLEU}")}
    t = Text()
    for r in range(H // 2):
        haut, bas = pix[2 * r], pix[2 * r + 1]
        txt, st = textes.get(r, ("", ""))
        debut = 26
        for x in range(larg):
            if txt and debut <= x < debut + len(txt):
                t.append(txt[x - debut], style=f"{st} on {bas[x]}")
            else:
                t.append("▀", style=f"{haut[x]} on {bas[x]}")
        if r < H // 2 - 1:
            t.append("\n")
    return t


def fmt(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 60}:{sec % 60:02d}"


def coupe(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def graphe(valeurs, largeur: int, hauteur: int):
    """Graphe braille (valeurs 0..100, la plus récente à droite) -> liste de lignes."""
    largeur, hauteur = max(1, largeur), max(1, hauteur)
    v = ([0.0] * (2 * largeur) + list(valeurs))[-2 * largeur:]
    pts = [max(1, round(x / 100 * hauteur * 4)) for x in v]  # 1 point mini = ligne pointillée btop
    lignes = []
    for r in range(hauteur):
        dessous = (hauteur - 1 - r) * 4
        lignes.append("".join(
            chr(0x2800 | BR_G[min(4, max(0, pts[2 * i] - dessous))]
                | BR_D[min(4, max(0, pts[2 * i + 1] - dessous))])
            for i in range(largeur)
        ))
    return lignes


def titre_cadre(gauche: str, droite: str, largeur: int, milieu: str = "") -> str:
    """Titre de bordure façon btop : texte relié par des traits ─."""
    reste = max(2, largeur - 4 - len(gauche) - len(droite) - len(milieu))
    if milieu:
        a = reste // 2
        return f"{gauche}{'─' * a}{milieu}{'─' * (reste - a)}{droite}"
    return f"{gauche}{'─' * reste}{droite}"


def nom_cpu() -> str:
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        nom = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        return nom.replace("AMD ", "").replace(" Processor", "").replace("-Core", " coeurs")
    except Exception:
        return "CPU"


def infos_gpu():
    """(nom, vram totale en octets) de la plus grosse carte graphique, ou ("", 0)."""
    meilleur = ("", 0)
    try:
        import winreg
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        for i in range(16):
            try:
                k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base + "\\%04d" % i)
                taille = int(winreg.QueryValueEx(k, "HardwareInformation.qwMemorySize")[0])
                if taille > meilleur[1]:
                    meilleur = (winreg.QueryValueEx(k, "DriverDesc")[0], taille)
            except OSError:
                pass
    except Exception:
        pass
    return meilleur


def mesure_gpu():
    """(usage 3D en %, VRAM utilisée en octets) via les compteurs Windows. Prend ~1 s."""
    q = win32pdh.OpenQuery()
    try:
        compteurs = [("u", win32pdh.AddCounter(q, p)) for p in
                     win32pdh.ExpandCounterPath(r"\GPU Engine(*engtype_3D)\Utilization Percentage")]
        compteurs += [("m", win32pdh.AddCounter(q, p)) for p in
                      win32pdh.ExpandCounterPath(r"\GPU Adapter Memory(*)\Dedicated Usage")]
        win32pdh.CollectQueryData(q)
        time.sleep(0.5)
        win32pdh.CollectQueryData(q)
        u = m = 0.0
        for k, c in compteurs:
            try:  # un processus peut disparaître entre les deux mesures
                v = win32pdh.GetFormattedCounterValue(c, win32pdh.PDH_FMT_DOUBLE)[1]
            except Exception:
                continue
            if k == "u":
                u += v
            else:
                m = max(m, v)
        return min(100.0, u), m
    finally:
        win32pdh.CloseQuery(q)


class Piste:
    def __init__(self, chemin: Path):
        self.chemin = chemin
        self.titre = chemin.stem
        self.artiste = "Inconnu"
        self.album = ""
        self.annee = ""
        self.duree = 0.0
        self.explicite = False
        if mutagen:
            try:
                f = mutagen.File(chemin, easy=True)
                if f is not None:
                    self.titre = (f.get("title") or [self.titre])[0]
                    self.artiste = (f.get("artist") or [self.artiste])[0]
                    self.album = (f.get("album") or [""])[0]
                    self.annee = str(f.get("date", [""])[0])[:4]
                    self.duree = float(f.info.length)
                tags = mutagen.File(chemin).tags
                if tags is not None:
                    if "rtng" in tags:  # M4A iTunes
                        self.explicite = list(tags["rtng"])[:1] == [1]
                    elif "TXXX:ITUNESADVISORY" in tags:  # MP3
                        self.explicite = str(tags["TXXX:ITUNESADVISORY"]) == "1"
            except Exception:
                pass

    def pochette(self):
        """Retourne une image PIL ou None."""
        if not (mutagen and PILImage):
            return None
        try:
            f = mutagen.File(self.chemin)
            data = None
            if f is None:
                return None
            if hasattr(f, "pictures") and f.pictures:  # FLAC
                data = f.pictures[0].data
            elif f.tags is not None:
                for k in f.tags.keys():  # MP3 (ID3 APIC)
                    if str(k).startswith("APIC"):
                        data = f.tags[k].data
                        break
                if data is None and "covr" in f.tags:  # M4A
                    data = bytes(f.tags["covr"][0])
            if data:
                return PILImage.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            return None
        return None


def scanner(dossier: Path):
    pistes = []
    if dossier.exists():
        for f in sorted(dossier.rglob("*")):
            if f.suffix.lower() in EXTS:
                pistes.append(Piste(f))
            if len(pistes) >= 300:
                break
    return pistes


def charger_favoris() -> set:
    try:
        return set(json.loads(FAVORIS.read_text(encoding="utf-8")))
    except Exception:
        return set()


class Dashboard(App):
    TITLE = "Vice Dashboard"
    CSS = f"""
    Screen {{ background: {FOND}; color: #e5e1ff; }}
    #topbar {{ height: 1; padding: 0 1; }}
    #banniere {{ height: 5; }}
    #sys {{ height: 12; border: round {CADRE_SYS}; border-title-color: {BLEU}; }}
    #sysgauche {{ width: 1fr; }}
    #cpugraphe {{ height: 1fr; }}
    #gpugraphe {{ height: 4; }}
    #cpuinfo {{ width: 54; border: round {CADRE_SYS}; border-title-color: {ORANGE}; padding: 0 1; }}
    #mem {{ height: 9; border: round {CADRE_SYS}; border-title-color: {BLEU}; padding: 0 1; }}
    #fil {{ height: 1; padding: 0 1; }}
    #music {{ height: 1fr; min-height: 14; }}
    #left {{ width: 50%; }}
    #playlist {{ height: 1fr; border: round {ROSE}; border-title-color: {ORANGE}; background: {FOND}; }}
    #spectre {{ height: 10; border: round {ROSE}; padding: 0 1; }}
    #now {{ width: 1fr; border: round {ROSE}; padding: 0 1; }}
    #cover {{ height: 1fr; }}
    #info {{ height: 6; content-align: center middle; text-align: center; }}
    #controls {{ height: 2; padding: 0 1; }}
    DataTable {{ scrollbar-size-vertical: 1; }}
    DataTable > .datatable--cursor {{ background: #3a1240; }}
    DataTable > .datatable--hover {{ background: #1e0b2e; }}
    .cache {{ display: none; }}
    """
    BINDINGS = [
        ("q", "quit", "Quitter"),
        ("space", "pause", "Pause"),
        ("n", "suivant", "Suivant"),
        ("p", "precedent", "Précédent"),
        ("left", "recule", "-5s"),
        ("right", "avance", "+5s"),
        ("plus", "vol_plus", "Vol+"),
        ("minus", "vol_moins", "Vol-"),
        ("f", "favori", "Favori"),
        ("s", "aleatoire", "Aléatoire"),
        ("r", "repeter", "Répéter"),
        ("1", "panneau('sys')", "cpu"),
        ("2", "panneau('mem')", "mem"),
    ]

    def __init__(self, dossier: Path, titre: str = "VICE DASHBOARD", sous_titre: str = "sunset terminal"):
        super().__init__()
        self.titre, self.sous_titre = titre, sous_titre
        self.dossier = dossier
        self.pistes = []
        self.courant = -1
        self.offset = 0.0
        self.en_pause = False
        self.volume = 0.5
        self.niveaux = []
        self.sf_handle = None
        self.audio_ok = True
        self.aleatoire = False
        self.repeter = False
        self.favoris = charger_favoris()
        self.cpu_nom = nom_cpu()
        self.gpu_nom, self.vram_total = infos_gpu()
        self.gpu = (0.0, 0.0)
        self.h_cpu = deque(maxlen=600)
        self.h_gpu = deque(maxlen=600)
        self.h_mem = {k: deque(maxlen=600) for k in ("used", "available", "free")}
        self.cols = []

    # ---------- interface ----------
    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        yield Static(id="banniere")
        with Horizontal(id="sys"):
            with Vertical(id="sysgauche"):
                yield Static(id="cpugraphe")
                yield Static(id="gpugraphe")
            yield Static(id="cpuinfo")
        yield Static(id="mem")
        yield Static(id="fil")
        with Horizontal(id="music"):
            with Vertical(id="left"):
                yield DataTable(id="playlist", cursor_type="row", show_header=False)
                yield Static(id="spectre")
            with Vertical(id="now"):
                if CoverImage is not None and PILImage is not None:
                    yield CoverImage(self.image_vide(), id="cover")
                else:
                    yield Static("(pochette : installe textual-image + pillow)", id="cover")
                yield Static(id="info")
        yield Static(id="controls")

    @staticmethod
    def image_vide():
        return PILImage.new("RGB", (300, 300), (24, 16, 48)) if PILImage else None

    def on_mount(self):
        self.query_one("#cpuinfo").border_title = self.cpu_nom
        self.query_one("#mem").border_title = "²mem"
        self.query_one("#fil").update(Text.assemble(
            ("vice", f"bold {VIOLET}"), (" › Musique › ", GRIS), (self.dossier.name or str(self.dossier), "bold white")))
        try:
            pygame.mixer.init()
            pygame.mixer.music.set_volume(self.volume)
        except Exception:
            self.audio_ok = False
        self.pistes = scanner(self.dossier)
        t = self.query_one("#playlist", DataTable)
        self.cols = t.add_columns("n", "titre", "artiste", "fav", "e", "durée")
        for i in range(len(self.pistes)):
            t.add_row(*self.ligne(i), key=str(i))
        t.focus()
        psutil.cpu_percent(percpu=True)
        if win32pdh is not None:
            threading.Thread(target=self.boucle_gpu, daemon=True).start()
        self.set_interval(1.0, self.maj_systeme)
        self.set_interval(0.06, self.maj_spectre)
        self.set_interval(0.25, self.maj_lecteur)
        self.set_interval(0.2, self.maj_banniere)
        self.phase = 0
        self.call_after_refresh(self.maj_systeme)
        self.maj_lecteur()

    def maj_banniere(self):
        self.phase += 1
        w = self.query_one("#banniere")
        if w.size.width:
            w.update(banniere(w.size.width, self.phase, self.titre, self.sous_titre))

    def boucle_gpu(self):
        while True:
            try:
                self.gpu = mesure_gpu()
            except Exception:
                pass
            time.sleep(1.0)

    def ligne(self, i: int):
        p = self.pistes[i]
        joue = i == self.courant
        st = f"bold {ROSE}" if joue else "white"
        return [
            Text(("▶ " if joue else "  ") + f"{i + 1:>2}", style=f"bold {ROSE}" if joue else GRIS),
            Text(coupe(p.titre.upper(), 22), style=st),
            Text(coupe(p.artiste, 12), style=ORANGE),
            Text("★" if str(p.chemin) in self.favoris else " ", style=JAUNE),
            Text("E" if p.explicite else " ", style=f"bold {ROUGE}"),
            Text(fmt(p.duree), style=st),
        ]

    def maj_ligne(self, i: int):
        if 0 <= i < len(self.pistes):
            t = self.query_one("#playlist", DataTable)
            for col, val in zip(self.cols, self.ligne(i)):
                t.update_cell(str(i), col, val)

    # ---------- lecture ----------
    def lire(self, index: int, debut: float = 0.0):
        if not self.pistes or not self.audio_ok:
            return
        index %= len(self.pistes)
        p = self.pistes[index]
        try:
            pygame.mixer.music.load(str(p.chemin))
            pygame.mixer.music.play(start=debut)
        except Exception as e:
            self.notify(f"Lecture impossible : {e}", severity="error")
            return
        ancien = self.courant
        if index != ancien:
            self.sf_handle = None
            if sf:
                try:
                    self.sf_handle = sf.SoundFile(str(p.chemin))
                except Exception:
                    self.sf_handle = None  # (ex. m4a) -> spectre simulé
            self.maj_pochette(p)
        self.courant = index
        self.offset = debut
        self.en_pause = False
        self.maj_ligne(ancien)
        self.maj_ligne(index)
        self.query_one("#playlist", DataTable).move_cursor(row=index)

    def maj_pochette(self, p: Piste):
        img = p.pochette()
        w = self.query_one("#cover")
        if CoverImage is not None and PILImage is not None and isinstance(w, CoverImage):
            try:
                w.image = img if img is not None else self.image_vide()
            except Exception:
                pass

    def position(self) -> float:
        if self.courant < 0:
            return 0.0
        ms = pygame.mixer.music.get_pos()
        return self.offset + max(0, ms) / 1000.0

    def on_data_table_row_selected(self, event):
        self.lire(event.cursor_row)

    def index_suivant(self, pas: int) -> int:
        if self.courant < 0:
            return 0
        if self.aleatoire and len(self.pistes) > 1:
            return random.choice([i for i in range(len(self.pistes)) if i != self.courant])
        return self.courant + pas

    def action_pause(self):
        if self.courant < 0:
            self.lire(0)
            return
        if self.en_pause:
            pygame.mixer.music.unpause()
        else:
            pygame.mixer.music.pause()
        self.en_pause = not self.en_pause

    def action_suivant(self):
        self.lire(self.index_suivant(1))

    def action_precedent(self):
        self.lire(self.index_suivant(-1))

    def _seek(self, delta: float):
        if self.courant < 0:
            return
        d = self.pistes[self.courant].duree or 1e9
        cible = max(0.0, min(d - 1, self.position() + delta))
        self.lire(self.courant, cible)

    def action_avance(self):
        self._seek(5)

    def action_recule(self):
        self._seek(-5)

    def action_vol_plus(self):
        self.volume = min(1.0, self.volume + 0.05)
        pygame.mixer.music.set_volume(self.volume)

    def action_vol_moins(self):
        self.volume = max(0.0, self.volume - 0.05)
        pygame.mixer.music.set_volume(self.volume)

    def action_favori(self):
        i = self.query_one("#playlist", DataTable).cursor_row
        if not (0 <= i < len(self.pistes)):
            return
        cle = str(self.pistes[i].chemin)
        self.favoris ^= {cle}
        try:
            FAVORIS.write_text(json.dumps(sorted(self.favoris)), encoding="utf-8")
        except Exception as e:
            self.notify(f"Favori non enregistré : {e}", severity="warning")
        self.maj_ligne(i)

    def action_aleatoire(self):
        self.aleatoire = not self.aleatoire

    def action_repeter(self):
        self.repeter = not self.repeter

    def action_panneau(self, nom: str):
        self.query_one(f"#{nom}").toggle_class("cache")
        self.call_after_refresh(self.maj_systeme)  # attendre la nouvelle taille du panneau

    # ---------- monitoring ----------
    def maj_systeme(self):
        coeurs = psutil.cpu_percent(percpu=True)
        total = sum(coeurs) / len(coeurs)
        mem = psutil.virtual_memory()
        gpu_pct, vram = self.gpu
        vram_pct = 100 * vram / self.vram_total if self.vram_total else 0
        self.h_cpu.append(total)
        self.h_gpu.append(gpu_pct)
        for k in self.h_mem:
            self.h_mem[k].append(100 * getattr(mem, k) / mem.total)
        up = int(time.time() - psutil.boot_time())
        j, r = divmod(up, 86400)
        h, r = divmod(r, 3600)
        bat = psutil.sensors_battery()

        # barre du haut
        top = Text(" ⛨ ", style=ROSE)
        for num, nom, coul in (("1", "sys", ROSE), ("2", "mem", ORANGE)):
            visible = not self.query_one(f"#{nom}").has_class("cache")
            top.append(f" {num} ", style=f"bold #0a0614 on {coul}" if visible else f"{GRIS} on #1e0b2e")
            top.append(" ")
        top.append(f"  ◔ {total:.0f}%", style=BLEU)
        if win32pdh is not None:
            top.append(f"   ◎ {gpu_pct:.0f}%", style=ROSE)
        top.append(f"   ⛁ {mem.percent:.0f}%", style=ORANGE)
        if self.vram_total:
            top.append(f"   ▣ {vram_pct:.0f}%", style=VIOLET)
        self.query_one("#topbar").update(top)

        # cadre cpu (titre façon btop)
        sysw = self.query_one("#sys")
        droite = f"BAT {int(bat.percent)}%" if bat else f"up {j}j {h:02d}:{r // 60:02d}"
        sysw.border_title = titre_cadre("¹cpu", droite, sysw.size.width, time.strftime(" %H:%M:%S "))

        g = self.query_one("#cpugraphe")
        lignes = graphe(self.h_cpu, g.size.width, g.size.height)
        txt = Text()
        for n, l in enumerate(lignes):
            txt.append(l + "\n", style=degrade(GRAPHE, n / max(1, len(lignes) - 1)))
        g.update(txt)

        gg = self.query_one("#gpugraphe")
        larg = gg.size.width
        txt = Text(titre_cadre("", "", larg + 4, " total ▲▼ gpu ") + "\n", style=CADRE)
        if win32pdh is not None:
            for n, l in enumerate(graphe(self.h_gpu, larg, 2)):
                txt.append(l + "\n", style=ROSE if n == 0 else ORANGE)
            txt.append(f"GPU0 {coupe(self.gpu_nom, 30)}", style=GRIS)
        else:
            txt.append("\n(GPU : installe pywin32)", style=GRIS)
        gg.update(txt)

        # cadre infos cpu
        info = self.query_one("#cpuinfo")
        freq = psutil.cpu_freq()
        info.border_title = titre_cadre(coupe(self.cpu_nom, 30), f"{freq.current / 1000:.1f} GHz" if freq else "",
                                        info.size.width)
        t = Text()
        t.append("CPU ", style=f"bold {ROSE}")
        t.append(barre(total, 22))
        t.append(f" {total:3.0f}% ", style="white")
        t.append(graphe(self.h_cpu, 12, 1)[0] + "\n", style=BLEU)
        nb = len(coeurs)
        par_col = (nb + 2) // 3
        for rang in range(par_col):
            for c in range(3):
                k = c * par_col + rang
                if k < nb:
                    t.append(f"C{k:<2}", style=f"bold {ROSE}")
                    t.append(" ")
                    t.append(barre(coeurs[k], 5))
                    t.append(f" {coeurs[k]:3.0f}%", style="white")
                    if c < 2:
                        t.append(" │", style=CADRE)
            t.append("\n")
        la = psutil.getloadavg()
        t.append(f"{'Load avg:':>21} {la[0]:5.2f} {la[1]:5.2f} {la[2]:5.2f}\n", style=GRIS)
        if win32pdh is not None:
            t.append("GPU0 ", style=f"bold {ROSE}")
            t.append(barre(gpu_pct, 10, (ORANGE, ROSE)))
            t.append(f" {gpu_pct:3.0f}% ", style="white")
            t.append(graphe(self.h_gpu, 8, 1)[0], style=ORANGE)
            if self.vram_total:
                t.append(f"  {vram / 1024 ** 3:.1f}G/{self.vram_total / 1024 ** 3:.0f}G", style=GRIS)
        info.update(t)

        # cadre mémoire
        mw = self.query_one("#mem")
        larg = max(10, mw.size.width - 4)
        go = 1024 ** 3
        txt = Text()
        txt.append("Total:", style=f"bold {ROSE}")
        txt.append(f"{mem.total / go:>{larg - 9}.1f} Go\n", style="white")
        for nom, cle, coul in (("Used:", "used", ROSE), ("Available:", "available", BLEU),
                               ("Free:", "free", ORANGE)):
            val = getattr(mem, cle) / go
            pct = self.h_mem[cle][-1]
            txt.append(nom, style=f"bold {ROSE}")
            txt.append(f"{pct:>{larg - len(nom) - 12}.0f}%  {val:5.1f} Go\n", style="white")
            txt.append(graphe(self.h_mem[cle], larg, 1)[0] + "\n", style=coul)
        mw.update(txt)

    # ---------- spectre ----------
    def bandes_reelles(self, n: int):
        """FFT de la fenêtre audio à la position courante, n bandes log."""
        if not (sf and self.sf_handle and np is not None):
            return None
        try:
            sr = self.sf_handle.samplerate
            self.sf_handle.seek(max(0, int(self.position() * sr)))
            bloc = self.sf_handle.read(2048, dtype="float32", always_2d=True)
            if len(bloc) < 2048:
                return None
            mono = bloc.mean(axis=1) * np.hanning(2048)
            spec = np.abs(np.fft.rfft(mono))
            freqs = np.fft.rfftfreq(2048, 1.0 / sr)
            bords = np.logspace(np.log10(40), np.log10(min(16000, sr / 2)), n + 1)
            vals = []
            for a, b in zip(bords[:-1], bords[1:]):
                m = (freqs >= a) & (freqs < b)
                vals.append(spec[m].max() if m.any() else 0.0)
            v = np.log10(1 + np.array(vals) * 20) / 2.2
            return np.clip(v, 0, 1).tolist()
        except Exception:
            return None

    def maj_spectre(self):
        w = self.query_one("#spectre")
        n = max(8, (w.size.width - 2) // 3)
        haut = max(2, w.size.height - 2)
        if len(self.niveaux) != n:
            self.niveaux = [0.05] * n
        jouant = self.courant >= 0 and not self.en_pause and pygame.mixer.music.get_busy()
        if jouant:
            cible = self.bandes_reelles(n)
            if cible is None:
                cible = [random.random() * 0.8 for _ in range(n)]
        else:
            cible = [0.03] * n
        self.niveaux = [max(c, old - 0.08) for old, c in zip(self.niveaux, cible)]
        blocs = " ▁▂▃▄▅▆▇█"
        txt = Text()
        for r in range(haut):
            dessous = (haut - 1 - r) * 8
            ligne = "".join(blocs[min(8, max(0, int(v * haut * 8) - dessous))] * 2 + " " for v in self.niveaux)
            txt.append(ligne + ("\n" if r < haut - 1 else ""),
                       style=DEGRADE[min(len(DEGRADE) - 1, r * len(DEGRADE) // haut)])
        w.update(txt)

    # ---------- affichage lecteur ----------
    def maj_lecteur(self):
        t = self.query_one("#playlist", DataTable)
        t.border_title = titre_cadre(f" {self.dossier.name or 'Musique'} ", f" {len(self.pistes)} titres ",
                                     t.size.width)
        info = self.query_one("#info")
        controls = self.query_one("#controls")
        larg = max(20, controls.size.width - 2)
        aides = [("entrée", "lire"), ("espace", "pause"), ("n/p", "titre"),
                 ("←→", "±5s"), ("+/-", "vol"), ("f", "favori"), ("s", "aléa"), ("r", "répéter"),
                 ("1/2", "panneaux"), ("q", "quitter")]
        aide = Text()
        for k, v in aides:
            aide.append(k, style=f"bold {VIOLET}")
            aide.append(f" {v}  ", style=GRIS)
        if self.courant < 0:
            info.update(Text("Choisis un titre (entrée)", style=GRIS))
            pos, duree = 0.0, 0.0
        else:
            p = self.pistes[self.courant]
            # passage automatique au titre suivant
            if not self.en_pause and not pygame.mixer.music.get_busy():
                self.lire(self.courant if self.repeter else self.index_suivant(1))
                return
            pos, duree = self.position(), p.duree
            etat = " ⏸ En pause " if self.en_pause else " ▶ Lecture en cours "
            info.update(Text.assemble(
                (p.titre.upper() + "\n", "bold white"),
                (p.artiste + "\n", f"bold {ORANGE}"),
                ((p.album or " ") + "\n", f"italic {BLEU}"),
                ((p.annee or " ") + "\n", GRIS),
                (etat, f"bold #0a0614 on {ROSE}"),
            ))
        en_cours = self.courant >= 0 and not self.en_pause
        fav = self.courant >= 0 and str(self.pistes[self.courant].chemin) in self.favoris
        ligne = Text()
        ligne.append(f"⏮  {'⏸' if en_cours else '▶'}  ⏭  ", style=ROSE)
        ligne.append("★", style=JAUNE if fav else CADRE)
        droite = f"  {fmt(duree)}  "
        fin = f"  ⇄  ↺     Vol {int(self.volume * 100)}%"
        lb = max(5, larg - 10 - len(fmt(pos)) - len(droite) - len(fin) - 2)
        k = int(lb * min(1.0, pos / duree)) if duree else 0
        ligne.append(f"  {fmt(pos)}  ", style="white")
        for i in range(k):
            ligne.append("━", style=degrade((ORANGE, ROSE, VIOLET), i / max(1, lb - 1)))
        ligne.append("●", style="bold white")
        ligne.append("─" * (lb - k), style=CADRE)
        ligne.append(droite, style="white")
        ligne.append("⇄", style=f"bold {ROSE}" if self.aleatoire else CADRE)
        ligne.append("  ")
        ligne.append("↺", style=f"bold {ROSE}" if self.repeter else CADRE)
        ligne.append(f"     Vol {int(self.volume * 100)}%\n", style=VIOLET)
        controls.update(ligne + aide)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Monitoring système + lecteur audio, thème coucher de soleil néon.")
    ap.add_argument("dossier", nargs="?", default=str(Path.home() / "Music"), help="dossier de musique")
    ap.add_argument("--titre", default="VICE DASHBOARD", help="texte néon du bandeau")
    ap.add_argument("--sous-titre", default="sunset terminal", help="sous-titre du bandeau")
    a = ap.parse_args()
    Dashboard(Path(a.dossier), a.titre, a.sous_titre).run()
