#!/usr/bin/env python3
"""
STEM Probe Simulator GUI  —  version 0.5
Physics core adapted from notebook by J. Barthel (ju.barthel@fz-juelich.de)
Ernst Ruska-Centre (ER-C 2), Forschungszentrum Jülich GmbH, 2025

Version history
---------------
0.1  Initial release.  Wave-optical probe calculation, phase plate display,
     d50 metric, basic aberration table.

0.2  Added Resolution vs α sweep (Tab 2) and Resolution vs Defocus sweep
     (Tab 3).  Progress bars and threaded calculation to keep GUI responsive.

0.3  Scrollable left panel; extended aberration table to 6th-order terms
     (C5, A5, R5, S5).  Colormap selector and additional probe display modes
     (contour, 3-D surface, x/y line profiles).

0.4  Focus-spread and source-size envelopes added.  Both TEM and STEM Scherzer
     defocus values computed and displayed in the probe tab info bar.  Live
     wavelength and dk readouts.  Dual Scherzer lines marked on the defocus
     sweep plot.

0.5  Generalised Scherzer optimum based on Weyland & Muller (2004).
     - New physics functions for arbitrary phase-error tolerance φ:
         Δf  = −(2|φ|/π)^½ · (Cs·λ)^½
         α₀  = (8|φ|·λ / (π·Cs))^¼
         d₀  = 0.61·(π/(8|φ|))^¼ · Cs^¼ · λ^¾
     - "Scherzer Optimal Probe" panel: live φ input (default −π/2) with
       formula labels and computed values for d₀, α₀, and Δf.  Changing φ,
       energy, or Cs auto-updates the convergence-angle and defocus fields.
     - Default Cs (C₃) changed to 2 100 000 nm (2.1 mm); all other
       aberrations (coma, astigmatisms, C5) default to zero.
     - Defocus sweep and probe-tab Scherzer display both use the generalised
       formula so they respect the user-chosen φ.
"""

VERSION = "0.5"

import numpy as np
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import matplotlib
matplotlib.use('TkAgg')
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401 — registers 3-D projection


# ============================================================
# Physics core
# ============================================================

def calc_wavelength(e_kev):
    """Relativistic electron wavelength in nm from beam energy in keV."""
    return 1.2398419843320025 / np.sqrt(e_kev * (e_kev + 1022.0))


def calc_scherzer_tem(cs_nm, wl_nm):
    """
    Scherzer defocus for TEM phase-contrast imaging (nm).
    Maximises the CTF passband:  Δf_S(TEM) = −√(4/3 · Cs · λ)
    Returns None if Cs ≤ 0.
    """
    if cs_nm <= 0:
        return None
    return -np.sqrt((4.0 / 3.0) * cs_nm * wl_nm)


def calc_scherzer_stem(cs_nm, wl_nm):
    """
    Scherzer defocus for STEM probe optimisation (nm).
    Minimises the probe diameter:  Δf_S(STEM) = −√(Cs · λ)
    Returns None if Cs ≤ 0.
    """
    if cs_nm <= 0:
        return None
    return -np.sqrt(cs_nm * wl_nm)


def calc_scherzer_stem_general(cs_nm, wl_nm, phi_max):
    """
    Generalised STEM Scherzer defocus for an arbitrary maximum phase error φ_max (rad).
      Δf = −(2|φ_max|/π)^(1/2) · (Cs · λ)^(1/2)
    Recovers calc_scherzer_stem when φ_max = −π/2.
    Returns None if Cs ≤ 0 or φ_max = 0.
    """
    if cs_nm <= 0 or phi_max == 0.0:
        return None
    return -np.sqrt(2.0 * abs(phi_max) / np.pi) * np.sqrt(cs_nm * wl_nm)


def calc_optimal_alpha_mrad(cs_nm, wl_nm, phi_max):
    """
    Optimal convergence semi-angle in mrad for an arbitrary phase error φ_max (rad).
      α₀ = (8|φ_max| · λ / (π · Cs))^(1/4)
    """
    if cs_nm <= 0 or phi_max == 0.0:
        return None
    return 1000.0 * (8.0 * abs(phi_max) * wl_nm / (np.pi * cs_nm)) ** 0.25


def calc_optimal_probe_size(cs_nm, wl_nm, phi_max):
    """
    Optimal probe FWHM in nm for an arbitrary phase error φ_max (rad).
      d₀ = 0.61 · (π / (8|φ_max|))^(1/4) · Cs^(1/4) · λ^(3/4)
    """
    if cs_nm <= 0 or phi_max == 0.0:
        return None
    return 0.61 * (np.pi / (8.0 * abs(phi_max))) ** 0.25 * cs_nm ** 0.25 * wl_nm ** 0.75


def get_aperture(radius, edge_width, x, y):
    return 0.5 * (1.0 - np.tanh((np.sqrt(x**2 + y**2) - radius) / edge_width))


def get_phaseplate(aberrations, aperture, kx, ky, wavelength):
    ap_mask = aperture > 0.01
    act_idx = np.flatnonzero(ap_mask)
    kx_act  = kx.flat[act_idx]
    ky_act  = ky.flat[act_idx]
    k       = kx_act + 1j * ky_act
    k_cg    = kx_act - 1j * ky_act
    phi_act = np.zeros(len(act_idx), dtype=float)
    for (m, n), (ax, ay) in aberrations.items():
        a = ax + 1j * ay
        p = (m + n) >> 1
        q = (m - n) >> 1
        phi_act += wavelength ** (m - 1) * (a * (k ** p) * (k_cg ** q)).real / m
    phi = np.zeros_like(kx, dtype=float)
    phi.flat[act_idx] = phi_act * 2.0 * np.pi
    return phi


def get_probe(aberrations, aperture, kx, ky, wavelength,
              focus_spread=None, source_size=None):
    ap_mask = aperture > 0.01
    act_idx = np.flatnonzero(ap_mask)
    kx_act  = kx.flat[act_idx]
    ky_act  = ky.flat[act_idx]
    k       = kx_act + 1j * ky_act
    k_cg    = kx_act - 1j * ky_act
    phi_act = np.zeros(len(act_idx), dtype=float)
    for (m, n), (ax, ay) in aberrations.items():
        a = ax + 1j * ay
        p = (m + n) >> 1
        q = (m - n) >> 1
        phi_act += wavelength ** (m - 1) * (a * (k ** p) * (k_cg ** q)).real / m
    phi      = np.zeros_like(kx, dtype=float)
    intens_r = np.zeros_like(kx, dtype=float)
    if focus_spread is not None:
        dfs = focus_spread / 5.0
        k2  = (k * k_cg).real
        for ifs in range(-10, 11):
            df     = dfs * ifs
            pf     = np.exp(-df ** 2 / focus_spread ** 2)
            phi_fs = df * wavelength * k2 / 2.0
            phi.flat[act_idx] = (phi_act + phi_fs) * 2.0 * np.pi
            wave_k   = aperture * np.exp(-1j * phi)
            intens_r += pf * np.abs(np.fft.ifft2(wave_k)) ** 2
    else:
        phi.flat[act_idx] = phi_act * 2.0 * np.pi
        wave_k   = aperture * np.exp(-1j * phi)
        intens_r = np.abs(np.fft.ifft2(wave_k)) ** 2
    if source_size is not None:
        sk       = np.exp(-np.pi ** 2 * source_size ** 2 * (kx ** 2 + ky ** 2))
        intens_r = np.fft.ifft2(np.fft.fft2(intens_r) * sk).real
    total = np.sum(intens_r)
    return intens_r / total if total > 0 else intens_r


def get_d50(pdf, dx):
    """d50 probe diameter in nm."""
    ndim = pdf.shape
    ix   = (np.fft.fftfreq(ndim[1]) * ndim[1]).astype(int)
    a_x  = np.tile(ix, (ndim[0], 1)) * dx
    a_y  = a_x.T
    r        = 5.0 * dx
    r_step   = 0.5 * r
    itmax    = 100
    it       = 0
    int_r    = 0.0
    while it < itmax and abs(r_step) > 1e-4 * dx and abs(int_r - 0.5) > 1e-4:
        r_disk = get_aperture(r, dx, a_x, a_y)
        int_r  = np.sum(pdf * r_disk)
        if (int_r > 0.5 and r_step > 0.0) or (int_r < 0.5 and r_step < 0.0):
            r_step = -0.5 * r_step
        r  += r_step
        it += 1
    return 2.0 * r


# ============================================================
# Aberration table — 4 naming conventions per entry
# ============================================================
ABERRATION_DEFS = [
    # (m,n)  Krivanek  Haider/TD    Traditional name                 def_ax   def_ay  init_on
    ((2, 0), 'C\u2081\u2080',  '\u0394f / C\u2081', 'Defocus',                       0.0,      0.0,   True),
    ((2, 2), 'C\u2081\u2082',  'A\u2081',            '2-fold astigmatism',            0.0,      0.1,   True),
    ((3, 1), 'C\u2082\u2081',  'B\u2082',            'Coma',                           0.0,      0.0,   True),
    ((3, 3), 'C\u2082\u2083',  'A\u2082',            '3-fold astigmatism',             0.0,      0.0,   True),
    ((4, 0), 'C\u2083\u2080',  'Cs / C\u2083',       'Spherical aberration (3rd)',  2100000.0,  0.0,   True),
    ((4, 2), 'C\u2083\u2082',  'S\u2083',            'Star aberration',               0.0,      0.0,   False),
    ((4, 4), 'C\u2083\u2084',  'A\u2083',            '4-fold astigmatism',            0.0,      0.0,   False),
    ((5, 1), 'C\u2084\u2081',  'B\u2084',            '4th-order coma',                0.0,      0.0,   False),
    ((5, 3), 'C\u2084\u2083',  'D\u2084',            '3-lobe aberration',             0.0,      0.0,   False),
    ((5, 5), 'C\u2084\u2085',  'A\u2084',            '5-fold astigmatism',            0.0,      0.0,   False),
    ((6, 0), 'C\u2085\u2080',  'C\u2085',            'Spherical aberration (5th)',    0.0,      0.0,   True),
    ((6, 2), 'C\u2085\u2082',  'S\u2085',            'Star aberration (6th order)',   0.0,      0.0,   False),
    ((6, 4), 'C\u2085\u2084',  'R\u2085',            'Rosette aberration',            0.0,      0.0,   False),
    ((6, 6), 'C\u2085\u2086',  'A\u2085',            '6-fold astigmatism',            0.0,      0.0,   True),
]

# Colormap options for the probe display
COLORMAP_OPTIONS = [
    ('Inferno',     'inferno'),
    ('Viridis',     'viridis'),
    ('Hot',         'hot'),
    ('Plasma',      'plasma'),
    ('Cube Helix',  'cubehelix'),
]


# ============================================================
# GUI
# ============================================================

class STEMProbeApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"STEM Probe Simulator  —  v{VERSION}")
        self.root.geometry("1480x860")
        self.root.minsize(1100, 680)

        self._calc_running  = False
        self._res_running   = False
        self._defoc_running = False
        self._probe_render_data = None

        self._setup_style()
        self._build_layout()

        # Set default defocus to STEM Scherzer after all widgets exist
        self._set_default_scherzer_defocus()

    # ------------------------------------------------------------------
    # Style
    # ------------------------------------------------------------------

    def _setup_style(self):
        s = ttk.Style()
        try:
            s.theme_use('clam')
        except Exception:
            pass
        s.configure('Title.TLabel',      font=('Segoe UI', 11, 'bold'))
        s.configure('Header.TLabel',     font=('Segoe UI', 9, 'bold'))
        s.configure('Info.TLabel',       font=('Segoe UI', 8),  foreground='#666666')
        s.configure('Warn.TLabel',       font=('Segoe UI', 8),  foreground='#b85c00')
        s.configure('Dk.TLabel',         font=('Segoe UI', 8),  foreground='#444488')
        s.configure('ScherzerTEM.TLabel',font=('Segoe UI', 9, 'bold'), foreground='#8b1a1a')
        s.configure('ScherzerSTEM.TLabel',font=('Segoe UI', 9, 'bold'), foreground='#1a6e1a')
        s.configure('TLabelframe.Label', font=('Segoe UI', 9, 'bold'))
        s.configure('Accent.TButton',    font=('Segoe UI', 10, 'bold'), padding=6)

    # ------------------------------------------------------------------
    # Top-level layout
    # ------------------------------------------------------------------

    def _build_layout(self):
        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # ---- Left: scrollable controls ----
        left_outer = ttk.Frame(paned, width=460)
        left_outer.pack_propagate(False)
        paned.add(left_outer, weight=0)

        self._left_canvas = tk.Canvas(left_outer, highlightthickness=0, bg='#f0f0f0')
        scrollbar = ttk.Scrollbar(left_outer, orient=tk.VERTICAL,
                                   command=self._left_canvas.yview)
        self._left_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._left_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.left_frame = ttk.Frame(self._left_canvas)
        self._left_win  = self._left_canvas.create_window(
            (0, 0), window=self.left_frame, anchor='nw')

        self.left_frame.bind('<Configure>', self._on_left_configure)
        self._left_canvas.bind('<Configure>', self._on_canvas_configure)
        self._left_canvas.bind('<MouseWheel>',
            lambda e: self._left_canvas.yview_scroll(-1 * (e.delta // 120), 'units'))

        # ---- Right: notebook tabs ----
        right_frame = ttk.Frame(paned)
        paned.add(right_frame, weight=1)

        self.notebook = ttk.Notebook(right_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self._build_physics_section()
        self._build_aberrations_section()
        self._build_action_buttons()
        self._build_probe_tab()
        self._build_resolution_tab()
        self._build_defocus_tab()

    def _on_left_configure(self, event):
        self._left_canvas.configure(scrollregion=self._left_canvas.bbox('all'))

    def _on_canvas_configure(self, event):
        self._left_canvas.itemconfig(self._left_win, width=event.width)

    # ------------------------------------------------------------------
    # Left panel — Physics parameters
    # ------------------------------------------------------------------

    def _build_physics_section(self):
        frame = ttk.LabelFrame(self.left_frame, text='Beam & Optics Parameters', padding=8)
        frame.pack(fill=tk.X, padx=8, pady=(10, 4))

        # Row helper
        def add_row(parent, label_text, var, row_idx, entry_width=10):
            ttk.Label(parent, text=label_text, width=26).grid(
                row=row_idx, column=0, sticky='w', padx=4, pady=2)
            ttk.Entry(parent, textvariable=var, width=entry_width).grid(
                row=row_idx, column=1, sticky='w', padx=4, pady=2)

        # --- Energy ---
        self.var_energy = tk.StringVar(value='300')
        add_row(frame, 'Beam energy (keV):', self.var_energy, 0)

        # --- Wavelength (derived) ---
        ttk.Label(frame, text='Wavelength (derived):').grid(
            row=1, column=0, sticky='w', padx=4, pady=2)
        self.var_wl_display = tk.StringVar(value='—')
        ttk.Label(frame, textvariable=self.var_wl_display,
                  style='Info.TLabel').grid(row=1, column=1, sticky='w', padx=4)
        self.var_energy.trace_add('write', self._update_wl_display)
        self._update_wl_display()

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(
            row=2, column=0, columnspan=3, sticky='ew', pady=4)

        # --- dx with live dk display ---
        self.var_dx = tk.StringVar(value='0.002')
        ttk.Label(frame, text='Sampling dx (nm/pixel):', width=26).grid(
            row=3, column=0, sticky='w', padx=4, pady=2)
        ttk.Entry(frame, textvariable=self.var_dx, width=10).grid(
            row=3, column=1, sticky='w', padx=4, pady=2)

        # dk formula label — shown on row below
        ttk.Label(frame, text='').grid(row=4, column=0)   # spacer
        self.var_dk_display = tk.StringVar(value='dk = 1 / (dx \u00d7 N) = —')
        ttk.Label(frame, textvariable=self.var_dk_display,
                  style='Dk.TLabel').grid(row=4, column=1, sticky='w', padx=4)

        # --- Grid size ---
        ttk.Label(frame, text='Grid size N (pixels):').grid(
            row=5, column=0, sticky='w', padx=4, pady=2)
        self.var_numk = tk.StringVar(value='512')
        ttk.Combobox(frame, textvariable=self.var_numk, width=8,
                     values=['256', '512', '1024', '2048', '4096'],
                     state='readonly').grid(row=5, column=1, sticky='w', padx=4, pady=2)

        # Attach traces for live dk update
        self.var_dx.trace_add('write',   self._update_dk_display)
        self.var_numk.trace_add('write', self._update_dk_display)
        self._update_dk_display()

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(
            row=6, column=0, columnspan=3, sticky='ew', pady=4)

        # --- Convergence angle ---
        self.var_alpha = tk.StringVar(value='10.0')
        add_row(frame, 'Conv. angle \u03b1 (mrad):', self.var_alpha, 7)
        ttk.Label(frame, text='(single probe calc & defocus sweep)',
                  style='Info.TLabel').grid(row=8, column=1, sticky='w', padx=4)

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(
            row=9, column=0, columnspan=3, sticky='ew', pady=4)

        # --- Focus spread ---
        ttk.Label(frame, text='Focus spread (nm):').grid(
            row=10, column=0, sticky='w', padx=4, pady=2)
        fs_row = ttk.Frame(frame)
        fs_row.grid(row=10, column=1, sticky='w')
        self.var_fs_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(fs_row, variable=self.var_fs_on, text='enable').pack(side=tk.LEFT)
        self.var_focus_spread = tk.StringVar(value='3.0')
        ttk.Entry(fs_row, textvariable=self.var_focus_spread, width=7).pack(
            side=tk.LEFT, padx=4)

        # --- Source size ---
        ttk.Label(frame, text='Source size (nm):').grid(
            row=11, column=0, sticky='w', padx=4, pady=2)
        ss_row = ttk.Frame(frame)
        ss_row.grid(row=11, column=1, sticky='w')
        self.var_ss_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(ss_row, variable=self.var_ss_on, text='enable').pack(side=tk.LEFT)
        self.var_source_size = tk.StringVar(value='0.03')
        ttk.Entry(ss_row, textvariable=self.var_source_size, width=7).pack(
            side=tk.LEFT, padx=4)

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(
            row=12, column=0, columnspan=3, sticky='ew', pady=4)

        # --- Scherzer / Optimal Probe subsection ---
        sch = ttk.LabelFrame(frame, text='Scherzer Optimal Probe', padding=6)
        sch.grid(row=13, column=0, columnspan=3, sticky='ew', padx=4, pady=4)

        # Phase error input
        ttk.Label(sch, text='Phase error \u03c6 (rad):', width=26).grid(
            row=0, column=0, sticky='w', padx=4, pady=2)
        self.var_phi_max = tk.StringVar(value=f'{-np.pi / 2:.6f}')
        ttk.Entry(sch, textvariable=self.var_phi_max, width=10).grid(
            row=0, column=1, sticky='w', padx=4, pady=2)
        ttk.Label(sch, text='(default \u2212\u03c0/2)', style='Info.TLabel').grid(
            row=0, column=2, sticky='w', padx=4)

        # d₀ formula label + live value
        ttk.Label(
            sch,
            text='d\u2080 = 0.61(\u03c0/(8|\u03c6|))\u00bc C\u209b\u00bc \u03bb\u00be :',
            style='Info.TLabel',
        ).grid(row=1, column=0, sticky='w', padx=4, pady=1)
        self.var_d0_display = tk.StringVar(value='\u2014')
        ttk.Label(sch, textvariable=self.var_d0_display,
                  style='ScherzerSTEM.TLabel').grid(
            row=1, column=1, columnspan=2, sticky='w', padx=4)

        # α₀ formula label + live value
        ttk.Label(
            sch,
            text='\u03b1\u2080 = (8|\u03c6|\u03bb/(\u03c0C\u209b))\u00bc :',
            style='Info.TLabel',
        ).grid(row=2, column=0, sticky='w', padx=4, pady=1)
        self.var_alpha0_display = tk.StringVar(value='\u2014')
        ttk.Label(sch, textvariable=self.var_alpha0_display,
                  style='ScherzerSTEM.TLabel').grid(
            row=2, column=1, columnspan=2, sticky='w', padx=4)

        # Δf formula label + live value
        ttk.Label(
            sch,
            text='\u0394f = \u2212(2|\u03c6|/\u03c0)\u00bd(C\u209b\u03bb)\u00bd :',
            style='Info.TLabel',
        ).grid(row=3, column=0, sticky='w', padx=4, pady=1)
        self.var_df0_display = tk.StringVar(value='\u2014')
        ttk.Label(sch, textvariable=self.var_df0_display,
                  style='ScherzerSTEM.TLabel').grid(
            row=3, column=1, columnspan=2, sticky='w', padx=4)

        # Traces: phi_max and energy → refresh display + set simulation fields
        self.var_phi_max.trace_add('write', self._update_scherzer_display)
        self.var_energy.trace_add('write', self._update_scherzer_display)

    def _update_wl_display(self, *_):
        try:
            e_kev = float(self.var_energy.get())
            if e_kev <= 0:
                raise ValueError
            wl = calc_wavelength(e_kev)
            self.var_wl_display.set(f'\u03bb = {wl*1000:.4f} pm  ({wl:.6f} nm)')
        except (ValueError, ZeroDivisionError):
            self.var_wl_display.set('—')

    def _update_dk_display(self, *_):
        try:
            dx = float(self.var_dx.get())
            n  = int(self.var_numk.get())
            if dx <= 0 or n <= 0:
                raise ValueError
            dk = 1.0 / (dx * n)
            self.var_dk_display.set(
                f'dk = 1 / (dx \u00d7 N) = {dk:.5f} nm\u207b\u00b9/pixel')
        except (ValueError, ZeroDivisionError):
            self.var_dk_display.set('dk = 1 / (dx \u00d7 N) = —')

    def _update_scherzer_display(self, *_):
        """
        Recompute Scherzer optimal d0, alpha0, Df from current phi, energy, and Cs.
        Also propagates alpha0 to var_alpha and Df to the defocus aberration field.
        """
        dash = '\u2014'
        try:
            phi = float(self.var_phi_max.get())
            wl  = calc_wavelength(float(self.var_energy.get()))
            cs  = self._get_cs_nm()
            if cs is None or cs <= 0 or phi == 0.0:
                raise ValueError('invalid params')

            d0     = calc_optimal_probe_size(cs, wl, phi)
            alpha0 = calc_optimal_alpha_mrad(cs, wl, phi)
            df0    = calc_scherzer_stem_general(cs, wl, phi)

            self.var_d0_display.set(
                f'{d0 * 1000:.2f} pm  ({d0:.5f} nm)')
            self.var_alpha0_display.set(f'{alpha0:.3f} mrad')
            self.var_df0_display.set(
                f'{df0:.4f} nm  ({df0 * 1000:.1f} pm)')

            # Push values into simulation fields
            self.var_alpha.set(f'{alpha0:.3f}')
            if hasattr(self, 'aber_rows') and (2, 0) in self.aber_rows:
                _, ax_var, _ = self.aber_rows[(2, 0)]
                ax_var.set(f'{df0:.4f}')
        except Exception:
            self.var_d0_display.set(dash)
            self.var_alpha0_display.set(dash)
            self.var_df0_display.set(dash)

    # ------------------------------------------------------------------
    # Left panel — Aberrations table
    # ------------------------------------------------------------------

    def _build_aberrations_section(self):
        outer = ttk.LabelFrame(self.left_frame,
                               text='Aberration Coefficients', padding=8)
        outer.pack(fill=tk.X, padx=8, pady=4)

        legend = ttk.LabelFrame(outer, text='Naming Conventions', padding=6)
        legend.pack(fill=tk.X, pady=(0, 6))

        conventions = [
            ('\u2460 Eikonal (m,n)',
             'Integer index pair used in this code.  m = aberration order,\n'
             'n = rotational symmetry.  m+n must be even, n \u2264 m.\n'
             'Example: (4,0) = 4th-order, rotationally symmetric = spherical aberration.'),
            ('\u2461 Krivanek notation',
             'C\u2099\u2098 where n = m\u208b\u2081 = wave-optical order, m = azimuthal symmetry.\n'
             'Two orthogonal components are labeled a/b (or x/y).\n'
             'Example: C\u2083\u2080 = 3rd-order spherical (= Cs),  C\u2081\u2082 = 2-fold astigmatism.'),
            ('\u2462 Typke\u2013Dierksen / Haider notation',
             'Used by CEOS correctors.  Letter prefix encodes symmetry type:\n'
             '  A = astigmatism,  B = coma,  C = spherical,  D = 3-lobe,\n'
             '  S = star,  R = rosette.\n'
             'Subscript = radial order (1-based).  Example: B\u2082 = coma, Cs = C\u2083.'),
            ('\u2463 Traditional / descriptive name',
             'Plain-language name widely used in the electron microscopy\n'
             'community (e.g. defocus, coma, spherical aberration).'),
        ]

        for title, body in conventions:
            row_f = ttk.Frame(legend)
            row_f.pack(fill=tk.X, pady=2)
            ttk.Label(row_f, text=title, style='Header.TLabel').pack(anchor='w')
            ttk.Label(row_f, text=body,  style='Info.TLabel',
                      wraplength=420, justify=tk.LEFT).pack(anchor='w', padx=12)

        ttk.Separator(outer, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=6)

        tbl = ttk.Frame(outer)
        tbl.pack(fill=tk.X)

        col_cfg = [
            ('On',              4,  'center'),
            ('\u2460 (m,n)',    6,  'center'),
            ('\u2461 Krivanek', 6,  'center'),
            ('\u2462 Haider',   8,  'center'),
            ('\u2463 Traditional name', 20, 'w'),
            ('Ax [nm]',        10,  'center'),
            ('Ay [nm]',        10,  'center'),
        ]
        for col, (text, width, anchor) in enumerate(col_cfg):
            ttk.Label(tbl, text=text, style='Header.TLabel',
                      width=width, anchor=anchor).grid(
                row=0, column=col, padx=2, pady=2, sticky='ew')

        ttk.Separator(tbl, orient=tk.HORIZONTAL).grid(
            row=1, column=0, columnspan=len(col_cfg), sticky='ew', pady=2)

        self.aber_rows = {}

        for r_idx, defn in enumerate(ABERRATION_DEFS, start=2):
            (m, n), krivanek, haider, trad, def_ax, def_ay, init_on = defn
            key = (m, n)

            en_var = tk.BooleanVar(value=init_on)
            ax_var = tk.StringVar(value=str(def_ax) if init_on else '0.0')
            ay_var = tk.StringVar(value=str(def_ay) if init_on else '0.0')

            ttk.Checkbutton(tbl, variable=en_var).grid(
                row=r_idx, column=0, padx=2, pady=1)
            ttk.Label(tbl, text=f'({m},{n})', anchor='center').grid(
                row=r_idx, column=1, padx=2)
            ttk.Label(tbl, text=krivanek, anchor='center').grid(
                row=r_idx, column=2, padx=2)
            ttk.Label(tbl, text=haider, anchor='center').grid(
                row=r_idx, column=3, padx=2)
            ttk.Label(tbl, text=trad, anchor='w').grid(
                row=r_idx, column=4, padx=4, sticky='w')
            ttk.Entry(tbl, textvariable=ax_var, width=10).grid(
                row=r_idx, column=5, padx=2)
            ttk.Entry(tbl, textvariable=ay_var, width=10).grid(
                row=r_idx, column=6, padx=2)

            self.aber_rows[key] = (en_var, ax_var, ay_var)

        # Refresh Scherzer display when Cs changes
        if (4, 0) in self.aber_rows:
            _, cs_ax, _ = self.aber_rows[(4, 0)]
            cs_ax.trace_add('write', self._update_scherzer_display)

    # ------------------------------------------------------------------
    # Left panel — Action buttons & status
    # ------------------------------------------------------------------

    def _build_action_buttons(self):
        frame = ttk.Frame(self.left_frame)
        frame.pack(fill=tk.X, padx=8, pady=8)

        self.btn_probe = ttk.Button(
            frame, text='  Calculate Probe  ',
            style='Accent.TButton', command=self._on_calc_probe)
        self.btn_probe.pack(fill=tk.X, pady=3)

        self.btn_res = ttk.Button(
            frame, text='  Calculate Resolution vs \u03b1  ',
            style='Accent.TButton', command=self._on_calc_resolution)
        self.btn_res.pack(fill=tk.X, pady=3)

        self.btn_defoc = ttk.Button(
            frame, text='  Calculate Resolution vs Defocus  ',
            style='Accent.TButton', command=self._on_calc_defocus)
        self.btn_defoc.pack(fill=tk.X, pady=3)

        self.progress = ttk.Progressbar(frame, mode='indeterminate', length=200)
        self.progress.pack(fill=tk.X, pady=4)

        self.var_status = tk.StringVar(value='Ready.')
        ttk.Label(frame, textvariable=self.var_status,
                  style='Info.TLabel', wraplength=420,
                  justify=tk.LEFT).pack(fill=tk.X)

    # ------------------------------------------------------------------
    # Right panel — Tab 1: Probe viewer
    # ------------------------------------------------------------------

    def _build_probe_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text=' Probe View ')

        # ---- Info bar: d50 + both Scherzer values ----
        info_bar = ttk.Frame(tab)
        info_bar.pack(fill=tk.X, padx=8, pady=(4, 0))

        ttk.Label(info_bar, text='d\u2085\u2080:', style='Header.TLabel').pack(side=tk.LEFT)
        self.var_d50 = tk.StringVar(value='—')
        ttk.Label(info_bar, textvariable=self.var_d50,
                  style='Title.TLabel').pack(side=tk.LEFT, padx=(4, 20))

        ttk.Label(info_bar, text='\u0394f\u209b(TEM):', style='Header.TLabel').pack(side=tk.LEFT)
        self.var_scherzer_tem_display = tk.StringVar(value='—')
        ttk.Label(info_bar, textvariable=self.var_scherzer_tem_display,
                  style='ScherzerTEM.TLabel').pack(side=tk.LEFT, padx=(4, 20))

        ttk.Label(info_bar, text='\u0394f\u209b(STEM):', style='Header.TLabel').pack(side=tk.LEFT)
        self.var_scherzer_stem_display = tk.StringVar(value='—')
        ttk.Label(info_bar, textvariable=self.var_scherzer_stem_display,
                  style='ScherzerSTEM.TLabel').pack(side=tk.LEFT, padx=4)

        # ---- Controls row: display mode + colormap ----
        ctrl_row = ttk.Frame(tab)
        ctrl_row.pack(fill=tk.X, padx=8, pady=4)

        mode_bar = ttk.LabelFrame(ctrl_row, text='Display mode', padding=4)
        mode_bar.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))

        self.var_probe_mode = tk.StringVar(value='intensity')
        for label, val in [('2D slice — x',  'slice_x'),
                            ('2D slice — y',  'slice_y'),
                            ('Intensity map', 'intensity'),
                            ('Contour map',   'contour'),
                            ('3D surface',    '3d')]:
            ttk.Radiobutton(mode_bar, text=label, variable=self.var_probe_mode,
                            value=val, command=self._on_probe_mode_change).pack(
                side=tk.LEFT, padx=8)

        cmap_bar = ttk.LabelFrame(ctrl_row, text='Colour scheme', padding=4)
        cmap_bar.pack(side=tk.LEFT, fill=tk.Y)

        self.var_colormap = tk.StringVar(value='inferno')
        for label, cmap in COLORMAP_OPTIONS:
            ttk.Radiobutton(cmap_bar, text=label, variable=self.var_colormap,
                            value=cmap, command=self._on_probe_mode_change).pack(
                side=tk.LEFT, padx=8)

        # ---- Figure ----
        self.fig_probe = Figure(figsize=(10, 4.5), tight_layout=True)
        self.ax_phase  = self.fig_probe.add_subplot(1, 2, 1)
        self.ax_probe  = self.fig_probe.add_subplot(1, 2, 2)

        canvas = FigureCanvasTkAgg(self.fig_probe, master=tab)
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        NavigationToolbar2Tk(canvas, tab).pack(fill=tk.X)
        self.canvas_probe = canvas

    # ------------------------------------------------------------------
    # Right panel — Tab 2: Resolution vs alpha
    # ------------------------------------------------------------------

    def _build_resolution_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text=' Resolution vs \u03b1 ')

        ctrl = ttk.LabelFrame(tab, text='Convergence angle sweep', padding=6)
        ctrl.pack(fill=tk.X, padx=8, pady=4)

        row = ttk.Frame(ctrl)
        row.pack(fill=tk.X)

        def _lbl(text): ttk.Label(row, text=text).pack(side=tk.LEFT, padx=(8, 2))
        def _ent(var, width=6):
            ttk.Entry(row, textvariable=var, width=width).pack(side=tk.LEFT)

        _lbl('\u03b1 min (mrad):');  self.var_amin   = tk.StringVar(value='5');  _ent(self.var_amin)
        _lbl('\u03b1 max (mrad):');  self.var_amax   = tk.StringVar(value='40'); _ent(self.var_amax)
        _lbl('Steps:');               self.var_asteps = tk.StringVar(value='20'); _ent(self.var_asteps, 5)

        self.var_fast_sweep = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, variable=self.var_fast_sweep,
                        text='Fast sweep (skip focus spread & source size)').pack(
            side=tk.LEFT, padx=10)

        self.btn_res2 = ttk.Button(row, text='Run Sweep',
                                    style='Accent.TButton',
                                    command=self._on_calc_resolution)
        self.btn_res2.pack(side=tk.LEFT, padx=10)

        self.res_progress = ttk.Progressbar(ctrl, mode='determinate', length=400)
        self.res_progress.pack(fill=tk.X, pady=4)

        self.fig_res = Figure(figsize=(10, 5), tight_layout=True)
        self.ax_res  = self.fig_res.add_subplot(1, 1, 1)
        self._init_res_axes()

        canvas = FigureCanvasTkAgg(self.fig_res, master=tab)
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        NavigationToolbar2Tk(canvas, tab).pack(fill=tk.X)
        self.canvas_res = canvas

    def _init_res_axes(self):
        self.ax_res.set_xlabel('Semi-convergence angle \u03b1 (mrad)', fontsize=11)
        self.ax_res.set_ylabel('Probe diameter d\u2085\u2080 (pm)', fontsize=11)
        self.ax_res.set_title('Probe resolution vs. convergence angle')
        self.ax_res.grid(True, alpha=0.3)

    # ------------------------------------------------------------------
    # Right panel — Tab 3: Resolution vs Defocus
    # ------------------------------------------------------------------

    def _build_defocus_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text=' Resolution vs Defocus ')

        ctrl = ttk.LabelFrame(tab,
                               text='Defocus sweep  (uses \u03b1 from Beam & Optics panel)',
                               padding=6)
        ctrl.pack(fill=tk.X, padx=8, pady=4)

        ttk.Label(ctrl,
                  text='Convergence angle \u03b1 is taken from the left panel.  '
                       'All other aberrations are held fixed; only \u0394f is swept.  '
                       'Both Scherzer lines are marked on the plot.',
                  style='Info.TLabel', wraplength=800, justify=tk.LEFT).pack(
            anchor='w', pady=(0, 4))

        param_row = ttk.Frame(ctrl)
        param_row.pack(fill=tk.X)

        def _lbl(text): ttk.Label(param_row, text=text).pack(side=tk.LEFT, padx=(8, 2))
        def _ent(var, width=8):
            ttk.Entry(param_row, textvariable=var, width=width).pack(side=tk.LEFT)

        _lbl('\u0394f min (nm):');  self.var_dfmin   = tk.StringVar(value='-50');  _ent(self.var_dfmin)
        _lbl('\u0394f max (nm):');  self.var_dfmax   = tk.StringVar(value='50');   _ent(self.var_dfmax)
        _lbl('Steps:');              self.var_dfsteps = tk.StringVar(value='30');   _ent(self.var_dfsteps, 5)

        self.var_fast_defoc = tk.BooleanVar(value=False)
        ttk.Checkbutton(param_row, variable=self.var_fast_defoc,
                        text='Fast (skip focus spread & source size)').pack(
            side=tk.LEFT, padx=10)

        self.btn_defoc2 = ttk.Button(param_row, text='Run Defocus Sweep',
                                      style='Accent.TButton',
                                      command=self._on_calc_defocus)
        self.btn_defoc2.pack(side=tk.LEFT, padx=10)

        self.defoc_progress = ttk.Progressbar(ctrl, mode='determinate', length=400)
        self.defoc_progress.pack(fill=tk.X, pady=4)

        alpha_row = ttk.Frame(ctrl)
        alpha_row.pack(fill=tk.X)
        ttk.Label(alpha_row, text='Using \u03b1 =', style='Header.TLabel').pack(
            side=tk.LEFT, padx=4)
        self.var_defoc_alpha_display = tk.StringVar(value='—')
        ttk.Label(alpha_row, textvariable=self.var_defoc_alpha_display,
                  style='Title.TLabel').pack(side=tk.LEFT)

        self.fig_defoc = Figure(figsize=(10, 5), tight_layout=True)
        self.ax_defoc  = self.fig_defoc.add_subplot(1, 1, 1)
        self._init_defoc_axes()

        canvas = FigureCanvasTkAgg(self.fig_defoc, master=tab)
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        NavigationToolbar2Tk(canvas, tab).pack(fill=tk.X)
        self.canvas_defoc = canvas

    def _init_defoc_axes(self):
        self.ax_defoc.set_xlabel('Defocus \u0394f (nm)', fontsize=11)
        self.ax_defoc.set_ylabel('Probe diameter d\u2085\u2080 (pm)', fontsize=11)
        self.ax_defoc.set_title('Probe resolution vs. defocus')
        self.ax_defoc.grid(True, alpha=0.3)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_aberrations(self):
        result = {}
        for (m, n), (en, vx, vy) in self.aber_rows.items():
            if en.get():
                try:
                    result[(m, n)] = (float(vx.get()), float(vy.get()))
                except ValueError:
                    pass
        return result

    def _get_physics(self):
        e_kev = float(self.var_energy.get())
        dx    = float(self.var_dx.get())
        num_k = int(self.var_numk.get())
        alpha = float(self.var_alpha.get())
        fs    = float(self.var_focus_spread.get()) if self.var_fs_on.get() else None
        ss    = float(self.var_source_size.get())  if self.var_ss_on.get() else None
        wl    = calc_wavelength(e_kev)
        return dict(e_kev=e_kev, dx=dx, num_k=num_k, alpha=alpha, wl=wl, fs=fs, ss=ss)

    def _get_cs_nm(self):
        """Return Cs (nm) from aberration table, or None if disabled/invalid."""
        en, vx, _ = self.aber_rows.get((4, 0), (None, None, None))
        if en is None or not en.get():
            return None
        try:
            return float(vx.get())
        except ValueError:
            return None

    def _compute_both_scherzer(self):
        """
        Returns (df_tem, df_stem, tem_str, stem_str).
        Values in nm; strings formatted to 3 decimal places.
        """
        try:
            wl = calc_wavelength(float(self.var_energy.get()))
        except (ValueError, ZeroDivisionError):
            return None, None, '—', '—'

        cs = self._get_cs_nm()
        if cs is None:
            return None, None, 'Cs disabled', 'Cs disabled'

        try:
            phi = float(self.var_phi_max.get())
        except Exception:
            phi = -np.pi / 2

        df_tem  = calc_scherzer_tem(cs, wl)
        df_stem = calc_scherzer_stem_general(cs, wl, phi)

        tem_str  = (f'{df_tem:.3f} nm  ({df_tem*1000:.1f} pm)'
                    if df_tem  is not None else 'Cs \u2264 0')
        stem_str = (f'{df_stem:.3f} nm  ({df_stem*1000:.1f} pm)'
                    if df_stem is not None else 'Cs \u2264 0')

        return df_tem, df_stem, tem_str, stem_str

    def _set_default_scherzer_defocus(self):
        """
        After GUI is built, initialise defocus, convergence angle and Scherzer
        display labels using the current phi_max, energy and Cs values.
        """
        self._update_scherzer_display()

    @staticmethod
    def _build_kgrid(num_k, dx):
        ikx = (np.fft.fftfreq(num_k) * num_k).astype(int)
        dk  = 1.0 / (dx * num_k)
        akx = np.tile(ikx, (num_k, 1)) * dk
        aky = akx.T
        return akx, aky, dk

    def _set_buttons(self, probe_state, res_state, defoc_state):
        self.btn_probe.config(state=probe_state)
        self.btn_res.config(state=res_state)
        self.btn_res2.config(state=res_state)
        self.btn_defoc.config(state=defoc_state)
        self.btn_defoc2.config(state=defoc_state)

    # ------------------------------------------------------------------
    # Probe view — mode / colormap switching
    # ------------------------------------------------------------------

    def _on_probe_mode_change(self):
        if self._probe_render_data is not None:
            self._render_probe_view(*self._probe_render_data)

    def _render_probe_view(self, phi, probe, d50, alpha, k_alpha, dk, alen, k_max):
        mode = self.var_probe_mode.get()
        cmap = self.var_colormap.get()

        # Rebuild axes — needed to switch 2-D ↔ 3-D projection
        self.fig_probe.clear()
        self.ax_phase = self.fig_probe.add_subplot(1, 2, 1)
        if mode == '3d':
            self.ax_probe = self.fig_probe.add_subplot(1, 2, 2, projection='3d')
        else:
            self.ax_probe = self.fig_probe.add_subplot(1, 2, 2)

        # ---- Phase plate (left, always 2-D, bwr) ----
        ax_p = self.ax_phase
        ax_p.imshow(np.fft.fftshift(np.sin(phi)), vmin=-1, vmax=1,
                    cmap='bwr', origin='lower',
                    extent=(-k_max, k_max - dk, -k_max, k_max - dk))
        ax_p.set_xlim(-1.5 * k_alpha, 1.5 * k_alpha)
        ax_p.set_ylim(-1.5 * k_alpha, 1.5 * k_alpha)
        ax_p.set_xlabel('$k_x$  [nm$^{-1}$]')
        ax_p.set_ylabel('$k_y$  [nm$^{-1}$]')
        ax_p.set_title(f'Phase plate  (\u03b1 = {alpha:.1f} mrad)')

        # ---- Probe view (right) ----
        ax_r     = self.ax_probe
        probe_sh = np.fft.fftshift(probe)
        ny, nx   = probe_sh.shape
        half     = max(5 * d50, 4 * dk)

        x_arr = np.linspace(-alen / 2, alen / 2, nx)
        y_arr = np.linspace(-alen / 2, alen / 2, ny)
        xi    = (np.searchsorted(x_arr, -half), np.searchsorted(x_arr, half))
        yi    = (np.searchsorted(y_arr, -half), np.searchsorted(y_arr, half))
        x_roi = x_arr[xi[0]:xi[1]]
        y_roi = y_arr[yi[0]:yi[1]]
        p_roi = probe_sh[yi[0]:yi[1], xi[0]:xi[1]]

        title_base = f'd\u2085\u2080 = {d50*1000:.2f} pm'

        if mode == 'intensity':
            im = ax_r.imshow(p_roi, vmin=0, cmap=cmap, origin='lower',
                             extent=(x_roi[0], x_roi[-1], y_roi[0], y_roi[-1]))
            self.fig_probe.colorbar(im, ax=ax_r, fraction=0.046, pad=0.04,
                                    label='Intensity (a.u.)')
            ax_r.set_xlabel('$x$  [nm]')
            ax_r.set_ylabel('$y$  [nm]')
            ax_r.set_title(f'Probe intensity  ({title_base})')

        elif mode == 'contour':
            X, Y = np.meshgrid(x_roi, y_roi)
            cs_plot = ax_r.contourf(X, Y, p_roi, levels=20, cmap=cmap)
            ax_r.contour(X, Y, p_roi, levels=20, colors='white',
                         linewidths=0.4, alpha=0.4)
            self.fig_probe.colorbar(cs_plot, ax=ax_r, fraction=0.046, pad=0.04,
                                    label='Intensity (a.u.)')
            ax_r.set_xlabel('$x$  [nm]')
            ax_r.set_ylabel('$y$  [nm]')
            ax_r.set_title(f'Probe contour  ({title_base})')
            ax_r.set_aspect('equal')

        elif mode == 'slice_x':
            cy = ny // 2
            ax_r.plot(x_arr, probe_sh[cy, :], color='steelblue', linewidth=1.5)
            ax_r.set_xlim(-half, half)
            ax_r.set_xlabel('$x$  [nm]')
            ax_r.set_ylabel('Intensity (a.u.)')
            ax_r.set_title(f'Horizontal slice (y = 0)  ({title_base})')
            ax_r.grid(True, alpha=0.3)
            ax_r.axvline(0, color='gray', lw=0.8, ls='--')

        elif mode == 'slice_y':
            cx = nx // 2
            ax_r.plot(probe_sh[:, cx], y_arr, color='darkorange', linewidth=1.5)
            ax_r.set_ylim(-half, half)
            ax_r.set_ylabel('$y$  [nm]')
            ax_r.set_xlabel('Intensity (a.u.)')
            ax_r.set_title(f'Vertical slice (x = 0)  ({title_base})')
            ax_r.grid(True, alpha=0.3)
            ax_r.axhline(0, color='gray', lw=0.8, ls='--')

        elif mode == '3d':
            step = max(1, p_roi.shape[0] // 128)
            p_ds = p_roi[::step, ::step]
            x_ds = x_roi[::step]
            y_ds = y_roi[::step]
            X, Y = np.meshgrid(x_ds, y_ds)
            ax_r.plot_surface(X, Y, p_ds, cmap=cmap,
                              rstride=1, cstride=1,
                              linewidth=0, antialiased=False)
            ax_r.view_init(elev=35, azim=45)
            ax_r.set_xlabel('$x$ [nm]')
            ax_r.set_ylabel('$y$ [nm]')
            ax_r.set_zlabel('Intensity')
            ax_r.set_title(f'3-D surface  ({title_base})')

        self.canvas_probe.draw()

    # ------------------------------------------------------------------
    # Calculate Probe
    # ------------------------------------------------------------------

    def _on_calc_probe(self):
        if self._calc_running or self._res_running or self._defoc_running:
            return
        self._calc_running = True
        self._set_buttons('disabled', 'disabled', 'disabled')
        self.var_status.set('Calculating probe…')
        self.progress.start(12)
        threading.Thread(target=self._calc_probe_thread, daemon=True).start()

    def _calc_probe_thread(self):
        try:
            phys  = self._get_physics()
            abers = self._get_aberrations()
            num_k = phys['num_k']
            dx    = phys['dx']
            wl    = phys['wl']
            alpha = phys['alpha']
            akx, aky, dk = self._build_kgrid(num_k, dx)
            k_alpha = 1e-3 * alpha / wl
            k_max   = dk * (num_k >> 1)
            alen    = dx * num_k

            warns = []
            if k_alpha > 0.66 * k_max:
                warns.append(f'Aperture edge ({k_alpha:.2f}/nm) near Nyquist ({k_max:.2f}/nm) — reduce dx.')
            if k_alpha / dk < 5:
                warns.append('Very few beams inside aperture — increase grid size or dx.')

            aperture = get_aperture(k_alpha, dk, akx, aky)
            phi      = get_phaseplate(abers, aperture, akx, aky, wl)
            probe    = get_probe(abers, aperture, akx, aky, wl,
                                 focus_spread=phys['fs'],
                                 source_size=phys['ss'])
            d50      = get_d50(probe, dx)

            self.root.after(0, self._update_probe_plot,
                             phi, probe, d50, alpha, k_alpha, dk, alen, k_max, warns)
        except Exception as exc:
            self.root.after(0, self._show_error, str(exc))
        finally:
            self.root.after(0, self._calc_probe_done)

    def _update_probe_plot(self, phi, probe, d50, alpha, k_alpha, dk, alen, k_max, warns):
        self._probe_render_data = (phi, probe, d50, alpha, k_alpha, dk, alen, k_max)

        # Update Scherzer displays (3 decimal places)
        df_tem, df_stem, tem_str, stem_str = self._compute_both_scherzer()
        self.var_scherzer_tem_display.set(tem_str)
        self.var_scherzer_stem_display.set(stem_str)

        self.var_d50.set(f'{d50 * 1000:.3f} pm   ({d50:.5f} nm)')
        self._render_probe_view(phi, probe, d50, alpha, k_alpha, dk, alen, k_max)

        status = 'Probe calculated.'
        if warns:
            status += '  \u26a0 ' + '  '.join(warns)
        self.var_status.set(status)
        self.notebook.select(0)

    def _calc_probe_done(self):
        self._calc_running = False
        self.progress.stop()
        self._set_buttons('normal', 'normal', 'normal')

    # ------------------------------------------------------------------
    # Calculate Resolution vs Alpha
    # ------------------------------------------------------------------

    def _on_calc_resolution(self):
        if self._calc_running or self._res_running or self._defoc_running:
            return
        self._res_running = True
        self._set_buttons('disabled', 'disabled', 'disabled')
        self.var_status.set('Calculating resolution curve…')
        self.progress.start(12)
        self.res_progress['value'] = 0
        threading.Thread(target=self._calc_res_thread, daemon=True).start()

    def _calc_res_thread(self):
        try:
            phys        = self._get_physics()
            abers       = self._get_aberrations()
            alpha_min   = float(self.var_amin.get())
            alpha_max   = float(self.var_amax.get())
            alpha_steps = max(2, int(self.var_asteps.get()))
            fast        = self.var_fast_sweep.get()

            alphas = np.linspace(alpha_min, alpha_max, alpha_steps)
            num_k  = phys['num_k']
            dx     = phys['dx']
            wl     = phys['wl']
            fs     = None if fast else phys['fs']
            ss     = None if fast else phys['ss']
            akx, aky, dk = self._build_kgrid(num_k, dx)

            d50s = []
            for i, alpha in enumerate(alphas):
                k_alpha  = 1e-3 * alpha / wl
                aperture = get_aperture(k_alpha, dk, akx, aky)
                probe    = get_probe(abers, aperture, akx, aky, wl,
                                     focus_spread=fs, source_size=ss)
                d50s.append(get_d50(probe, dx))
                pct = (i + 1) / len(alphas) * 100
                self.root.after(0, lambda p=pct: self.res_progress.configure(value=p))

            self.root.after(0, self._update_res_plot, alphas, d50s, phys, fast)
        except Exception as exc:
            self.root.after(0, self._show_error, str(exc))
        finally:
            self.root.after(0, self._calc_res_done)

    def _update_res_plot(self, alphas, d50s, phys, fast):
        self.ax_res.clear()

        d50_pm = [d * 1000 for d in d50s]
        self.ax_res.plot(alphas, d50_pm, 'o-',
                          color='steelblue', linewidth=2, markersize=5,
                          label='d\u2085\u2080')

        min_i     = int(np.argmin(d50s))
        min_alpha = alphas[min_i]
        min_d50   = d50_pm[min_i]
        d_range   = max(d50_pm) - min(d50_pm)
        a_range   = alphas[-1] - alphas[0]
        self.ax_res.plot(min_alpha, min_d50, 'r*', markersize=14, zorder=5)
        self.ax_res.annotate(
            f'min: {min_d50:.1f} pm\n\u03b1 = {min_alpha:.1f} mrad',
            xy=(min_alpha, min_d50),
            xytext=(min_alpha + 0.06 * a_range, min_d50 + 0.12 * d_range + 0.5),
            arrowprops=dict(arrowstyle='->', color='red', lw=1.4),
            fontsize=9, color='red',
        )

        e_kev  = phys['e_kev']
        wl     = phys['wl']
        suffix = '  [fast: no focus spread / source size]' if fast else ''
        self.ax_res.set_title(
            f'Probe resolution vs. convergence angle\n'
            f'E = {e_kev:.0f} keV,  \u03bb = {wl*1000:.4f} pm{suffix}',
            fontsize=10)
        self.ax_res.set_xlabel('Semi-convergence angle \u03b1 (mrad)', fontsize=11)
        self.ax_res.set_ylabel('Probe diameter d\u2085\u2080 (pm)', fontsize=11)
        self.ax_res.grid(True, alpha=0.3)
        self.ax_res.set_xlim(alphas[0] * 0.97, alphas[-1] * 1.03)
        self.ax_res.legend()
        self.canvas_res.draw()

        self.res_progress['value'] = 100
        self.var_status.set(
            f'Resolution curve done.  Optimum: d\u2085\u2080 = {min_d50:.1f} pm '
            f'at \u03b1 = {min_alpha:.1f} mrad')
        self.notebook.select(1)

    def _calc_res_done(self):
        self._res_running = False
        self.progress.stop()
        self._set_buttons('normal', 'normal', 'normal')

    # ------------------------------------------------------------------
    # Calculate Resolution vs Defocus  (Tab 3)
    # ------------------------------------------------------------------

    def _on_calc_defocus(self):
        if self._calc_running or self._res_running or self._defoc_running:
            return
        self._defoc_running = True
        self._set_buttons('disabled', 'disabled', 'disabled')
        try:
            alpha = float(self.var_alpha.get())
        except ValueError:
            alpha = 0.0
        self.var_defoc_alpha_display.set(f'{alpha:.2f} mrad')
        self.var_status.set('Calculating resolution vs defocus…')
        self.progress.start(12)
        self.defoc_progress['value'] = 0
        threading.Thread(target=self._calc_defocus_thread, daemon=True).start()

    def _calc_defocus_thread(self):
        try:
            phys       = self._get_physics()
            abers_base = {}
            for (m, n), (en, vx, vy) in self.aber_rows.items():
                if (m, n) == (2, 0):
                    continue
                if en.get():
                    try:
                        abers_base[(m, n)] = (float(vx.get()), float(vy.get()))
                    except ValueError:
                        pass

            df_min   = float(self.var_dfmin.get())
            df_max   = float(self.var_dfmax.get())
            df_steps = max(2, int(self.var_dfsteps.get()))
            fast     = self.var_fast_defoc.get()

            defoci   = np.linspace(df_min, df_max, df_steps)
            num_k    = phys['num_k']
            dx       = phys['dx']
            wl       = phys['wl']
            alpha    = phys['alpha']
            fs       = None if fast else phys['fs']
            ss       = None if fast else phys['ss']
            akx, aky, dk = self._build_kgrid(num_k, dx)
            k_alpha  = 1e-3 * alpha / wl
            aperture = get_aperture(k_alpha, dk, akx, aky)

            cs  = self._get_cs_nm()
            try:
                phi_sweep = float(self.var_phi_max.get())
            except Exception:
                phi_sweep = -np.pi / 2
            df_scherzer_tem  = calc_scherzer_tem(cs, wl) if cs is not None else None
            df_scherzer_stem = (calc_scherzer_stem_general(cs, wl, phi_sweep)
                                if cs is not None else None)

            d50s = []
            for i, df in enumerate(defoci):
                abers = dict(abers_base)
                abers[(2, 0)] = (df, 0.0)
                probe = get_probe(abers, aperture, akx, aky, wl,
                                  focus_spread=fs, source_size=ss)
                d50s.append(get_d50(probe, dx))
                pct = (i + 1) / len(defoci) * 100
                self.root.after(0, lambda p=pct: self.defoc_progress.configure(value=p))

            self.root.after(0, self._update_defoc_plot,
                             defoci, d50s, phys, fast,
                             df_scherzer_tem, df_scherzer_stem)
        except Exception as exc:
            self.root.after(0, self._show_error, str(exc))
        finally:
            self.root.after(0, self._calc_defocus_done)

    def _update_defoc_plot(self, defoci, d50s, phys, fast,
                            df_scherzer_tem, df_scherzer_stem):
        self.ax_defoc.clear()

        d50_pm  = [d * 1000 for d in d50s]
        f_range = defoci[-1] - defoci[0]
        d_range = max(d50_pm) - min(d50_pm) if max(d50_pm) != min(d50_pm) else 1.0

        self.ax_defoc.plot(defoci, d50_pm, 's-',
                            color='darkorange', linewidth=2, markersize=5,
                            label='d\u2085\u2080')

        # Minimum marker
        min_i   = int(np.argmin(d50s))
        min_df  = defoci[min_i]
        min_d50 = d50_pm[min_i]
        self.ax_defoc.plot(min_df, min_d50, 'r*', markersize=14, zorder=5)
        self.ax_defoc.annotate(
            f'min: {min_d50:.1f} pm\n\u0394f = {min_df:.2f} nm',
            xy=(min_df, min_d50),
            xytext=(min_df + 0.06 * f_range, min_d50 + 0.12 * d_range + 0.5),
            arrowprops=dict(arrowstyle='->', color='red', lw=1.4),
            fontsize=9, color='red',
        )

        # Scherzer TEM line (red-brown dashed)
        if df_scherzer_tem is not None:
            self.ax_defoc.axvline(df_scherzer_tem, color='#8b1a1a',
                                   linewidth=1.6, linestyle='--',
                                   label=f'\u0394f\u209b(TEM) = {df_scherzer_tem:.3f} nm')
            self.ax_defoc.text(
                df_scherzer_tem + 0.01 * f_range,
                min(d50_pm) + 0.55 * d_range,
                f'\u0394f\u209b(TEM)\n{df_scherzer_tem:.3f} nm',
                color='#8b1a1a', fontsize=8, va='bottom')

        # Scherzer STEM line (green dashed)
        if df_scherzer_stem is not None:
            self.ax_defoc.axvline(df_scherzer_stem, color='#1a6e1a',
                                   linewidth=1.6, linestyle='-.',
                                   label=f'\u0394f\u209b(STEM) = {df_scherzer_stem:.3f} nm')
            self.ax_defoc.text(
                df_scherzer_stem + 0.01 * f_range,
                min(d50_pm) + 0.25 * d_range,
                f'\u0394f\u209b(STEM)\n{df_scherzer_stem:.3f} nm',
                color='#1a6e1a', fontsize=8, va='bottom')

        # Zero-defocus reference
        self.ax_defoc.axvline(0, color='gray', linewidth=0.8,
                               linestyle=':', label='\u0394f = 0')

        e_kev  = phys['e_kev']
        wl     = phys['wl']
        alpha  = phys['alpha']
        suffix = '  [fast: no focus spread / source size]' if fast else ''
        self.ax_defoc.set_title(
            f'Probe resolution vs. defocus\n'
            f'E = {e_kev:.0f} keV,  \u03bb = {wl*1000:.4f} pm,  '
            f'\u03b1 = {alpha:.1f} mrad{suffix}',
            fontsize=10)
        self.ax_defoc.set_xlabel('Defocus \u0394f  (nm)', fontsize=11)
        self.ax_defoc.set_ylabel('Probe diameter d\u2085\u2080 (pm)', fontsize=11)
        self.ax_defoc.grid(True, alpha=0.3)
        self.ax_defoc.legend(fontsize=8)
        self.canvas_defoc.draw()

        self.defoc_progress['value'] = 100
        stem_note = (f'  \u0394f\u209b(STEM) = {df_scherzer_stem:.3f} nm'
                     if df_scherzer_stem is not None else '')
        self.var_status.set(
            f'Defocus sweep done.  Optimum: d\u2085\u2080 = {min_d50:.1f} pm '
            f'at \u0394f = {min_df:.2f} nm  (\u03b1 = {alpha:.1f} mrad){stem_note}')
        self.notebook.select(2)

    def _calc_defocus_done(self):
        self._defoc_running = False
        self.progress.stop()
        self._set_buttons('normal', 'normal', 'normal')

    # ------------------------------------------------------------------
    # Error
    # ------------------------------------------------------------------

    def _show_error(self, msg):
        self.var_status.set(f'Error: {msg}')
        messagebox.showerror('Calculation Error', msg)


# ============================================================
# Entry point
# ============================================================

def main():
    root = tk.Tk()
    app = STEMProbeApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
