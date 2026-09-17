"""霓虹主题 QSS（统一主题色，少量星空渐变点缀）。"""

BG = "#0B0F1A"
PANEL = "#141A2A"
CARD = "#1B2338"
PRIMARY = "#00E5FF"
ACCENT = "#B388FF"
TEXT = "#E6E9F0"
MUTED = "#7A8499"
ERROR = "#FF5252"
BORDER = "#232D47"

GLOBAL_QSS = f"""
* {{ font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; }}
QMainWindow, QWidget {{ background-color: {BG}; color: {TEXT}; }}
QFrame#panel {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                 stop:0 {PANEL}, stop:1 {CARD});
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QPushButton {{
    background-color: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 14px;
    min-height: 20px;
}}
QPushButton:hover {{ border-color: {PRIMARY}; color: {PRIMARY}; }}
QPushButton:checked {{ background-color: {PRIMARY}; color: {BG}; border-color: {PRIMARY}; }}
QComboBox, QSpinBox, QLineEdit {{
    background-color: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 8px;
    min-height: 20px;
}}
QComboBox:hover, QSpinBox:hover, QLineEdit:focus {{ border-color: {PRIMARY}; }}
QTableWidget {{
    background-color: {PANEL};
    gridline-color: {BORDER};
    alternate-background-color: {CARD};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
QHeaderView::section {{
    background-color: {CARD};
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 4px 8px;
}}
QStatusBar {{ background-color: {PANEL}; color: {MUTED}; }}
QLabel#sectionTitle {{ color: {PRIMARY}; font-size: 13px; font-weight: 600; }}
QCheckBox {{ color: {TEXT}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; }}
"""