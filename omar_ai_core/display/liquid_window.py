from __future__ import annotations

import sys
import threading
import time

import sounddevice as sd
try:
    from PyQt6.QtMultimedia import QMediaDevices
except ImportError:  # Optional on minimal Linux/Pi Qt installations.
    QMediaDevices = None
from PyQt6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QTimer,
    Qt,
    pyqtSignal,
    QUrl,
)
from PyQt6.QtGui import (
    QCloseEvent,
    QDesktopServices,
    QFont,
    QKeySequence,
    QPainterPath,
    QRegion,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from omar_ai_core.settings import (
    BASE_DIR,
    get_secret,
    is_configured,
    write_audio_devices,
    write_env,
)
from omar_ai_core.history import append_history, read_diagnostics, read_history
from omar_ai_core.memory.store import clear_memories, delete_memory, list_memories, remember

from .assistant_state import AssistantState, normalize_state, state_label
from .audio_reactive import AudioReactiveAnalyzer
from .liquid_renderer import LiquidGoldRenderer
from .visual_config import (
    MAX_ASSISTANT_SIZE,
    MAX_VISIBILITY,
    MIN_ASSISTANT_SIZE,
    MIN_VISIBILITY,
    RENDERER_PADDING,
    VisualSettings,
    estimated_core_diameter,
    load_visual_settings,
    save_visual_settings,
)


class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app

    def mainloop(self) -> None:
        self._app.exec()

    def protocol(self, *_args) -> None:
        return None


def _device_id_from_setting(name: str) -> int | None:
    value = get_secret(name)
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _friendly_audio_device_name(name: str, fallback: str) -> str:
    name = str(name or fallback).strip()
    primary, separator, details = name.partition(" (")
    primary = primary.strip()
    details = details.rstrip(")").strip()
    # "Altavoces" and "Micrófono" are generic endpoint roles. Windows shows
    # the useful hardware identity (for example "USB Audio and HID") directly
    # below them, so use that identity in Jarvis. Named displays/headsets keep
    # their real endpoint name.
    generic_roles = {
        "altavoces",
        "auriculares",
        "headphones",
        "microphone",
        "micrófono",
        "speakers",
    }
    if separator and details and primary.casefold() in generic_roles:
        return details
    return primary or details or fallback


def enumerate_audio_devices(
    direction: str,
    devices=None,
    hostapis=None,
) -> list[tuple[str, int]]:
    channel_key = "max_input_channels" if direction == "input" else "max_output_channels"
    source = sd.query_devices() if devices is None else devices
    if hostapis is None and devices is None:
        hostapis = sd.query_hostapis()
    preferred_hostapis = {
        index
        for index, hostapi in enumerate(hostapis or [])
        if "WASAPI" in str(hostapi.get("name", "")).upper()
    }
    result = []
    for index, device in enumerate(source):
        try:
            channels = int(device.get(channel_key, 0))
        except (AttributeError, TypeError, ValueError):
            continue
        if channels <= 0:
            continue
        if preferred_hostapis and device.get("hostapi") not in preferred_hostapis:
            continue
        name = _friendly_audio_device_name(
            device.get("name"),
            f"Dispositivo {index}",
        )
        result.append((name, index))
    return result


def _canonical_audio_device(
    selected_device: int | None,
    devices,
    candidates: list[tuple[str, int]],
    selected_label: str = "",
) -> int | None:
    if selected_device is None:
        return None
    candidate_ids = {device_id for _label, device_id in candidates}
    if selected_label:
        matching_ids = [
            device_id
            for label, device_id in candidates
            if label.casefold() == selected_label.casefold()
        ]
        if selected_device in matching_ids:
            return selected_device
        if matching_ids:
            return matching_ids[0]
        # A vanished saved endpoint must fall back to the Windows default.  Do
        # not silently bind to an unrelated device that reused its old index.
        return None
    if selected_device in candidate_ids and not selected_label:
        return selected_device
    try:
        old_device = devices[selected_device]
        old_name = _friendly_audio_device_name(old_device.get("name"), "").casefold()
    except (IndexError, KeyError, TypeError):
        return None
    for label, device_id in candidates:
        if label.casefold() == old_name:
            return device_id
    return None


class HistoryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Historial de Jarvis")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(720, 480)
        self.setMinimumSize(520, 340)
        self.setStyleSheet(
            """
            QDialog { background: #100c0e; color: #f6e3e6; }
            QLabel { color: #f6e3e6; font-size: 13px; font-weight: 600; }
            QTabWidget::pane {
                border: 1px solid rgba(232,154,36,105);
                background: #171315;
            }
            QTabBar::tab {
                color: rgba(244,220,224,210); background: #171315;
                border: 1px solid rgba(232,154,36,70);
                padding: 7px 14px;
            }
            QTabBar::tab:selected { color: white; background: #2a2020; }
            QPlainTextEdit {
                color: #f3e5e7; background: #171315; border: none;
                selection-background-color: #8c5a18;
                font-family: Consolas, "Courier New"; font-size: 11px;
            }
            QPushButton {
                color: #f6e3e6; background: rgba(255,255,255,12);
                border: 1px solid rgba(232,154,36,105); border-radius: 8px;
                padding: 5px 12px;
            }
            QPushButton:hover { background: rgba(232,154,36,40); }
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)
        root.addWidget(QLabel("HISTORIAL Y DIAGNÓSTICO"))

        tabs = QTabWidget()
        self.conversation = QPlainTextEdit()
        self.conversation.setReadOnly(True)
        self.conversation.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.diagnostics = QPlainTextEdit()
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        tabs.addTab(self.conversation, "CONVERSACIÓN")
        tabs.addTab(self.diagnostics, "ERRORES")
        root.addWidget(tabs, 1)

        actions = QHBoxLayout()
        actions.addStretch()
        refresh = QPushButton("ACTUALIZAR")
        refresh.clicked.connect(self.refresh)
        close = QPushButton("CERRAR")
        close.clicked.connect(self.close)
        actions.addWidget(refresh)
        actions.addWidget(close)
        root.addLayout(actions)
        self.refresh()

    def refresh(self) -> None:
        history = read_history() or "Todavía no hay conversaciones registradas."
        diagnostics = read_diagnostics() or "No hay errores registrados."
        self.conversation.setPlainText(history)
        self.diagnostics.setPlainText(diagnostics)
        self.conversation.moveCursor(self.conversation.textCursor().MoveOperation.End)
        self.diagnostics.moveCursor(self.diagnostics.textCursor().MoveOperation.End)


class MemoryDialog(QDialog):
    CATEGORIES = [
        "identity",
        "preferences",
        "projects",
        "relationships",
        "wishes",
        "notes",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Memoria de Jarvis")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(820, 500)
        self.setMinimumSize(620, 360)
        self.setStyleSheet(
            """
            QDialog { background: #100c0e; color: #f6e3e6; }
            QLabel { color: #f6e3e6; font-size: 13px; font-weight: 600; }
            QLineEdit, QTableWidget {
                color: #f3e5e7; background: #171315;
                border: 1px solid rgba(232,154,36,105);
                selection-background-color: #704514;
            }
            QLineEdit { border-radius: 8px; padding: 6px 9px; }
            QHeaderView::section {
                color: #f3e5e7; background: #2a2020;
                border: none; border-right: 1px solid rgba(232,154,36,55);
                padding: 6px;
            }
            QPushButton {
                color: #f6e3e6; background: rgba(255,255,255,12);
                border: 1px solid rgba(232,154,36,105); border-radius: 8px;
                padding: 5px 12px;
            }
            QPushButton:hover { background: rgba(232,154,36,40); }
            """
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("MEMORIA A LARGO PLAZO"))
        title_row.addStretch()
        self.count_label = QLabel("")
        self.count_label.setFont(QFont("Segoe UI", 9))
        title_row.addWidget(self.count_label)
        root.addLayout(title_row)

        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar recuerdos…")
        self.search.returnPressed.connect(self.refresh)
        search_row.addWidget(self.search, 1)
        refresh = QPushButton("BUSCAR")
        refresh.clicked.connect(self.refresh)
        search_row.addWidget(refresh)
        root.addLayout(search_row)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["CATEGORÍA", "CLAVE", "RECUERDO", "IMPORTANCIA", "ACTUALIZADO"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.doubleClicked.connect(self.edit_selected)
        root.addWidget(self.table, 1)

        actions = QHBoxLayout()
        add = QPushButton("AÑADIR")
        add.clicked.connect(self.add_memory)
        edit = QPushButton("EDITAR")
        edit.clicked.connect(self.edit_selected)
        delete = QPushButton("BORRAR")
        delete.clicked.connect(self.delete_selected)
        clear = QPushButton("BORRAR TODO")
        clear.clicked.connect(self.clear_all)
        close = QPushButton("CERRAR")
        close.clicked.connect(self.close)
        actions.addWidget(add)
        actions.addWidget(edit)
        actions.addWidget(delete)
        actions.addStretch()
        actions.addWidget(clear)
        actions.addWidget(close)
        root.addLayout(actions)
        self.refresh()

    def _selected_entry(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def refresh(self) -> None:
        entries = list_memories(self.search.text().strip(), limit=1000)
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            values = (
                entry["category"],
                entry["key"],
                entry["value"],
                f"{float(entry['importance']) * 100:.0f}%",
                str(entry["updated_at"])[:19].replace("T", " "),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry)
                self.table.setItem(row, column, item)
        self.count_label.setText(f"{len(entries)} recuerdos")

    def add_memory(self) -> None:
        category, accepted = QInputDialog.getItem(
            self, "Nuevo recuerdo", "Categoría:", self.CATEGORIES, 5, False
        )
        if not accepted:
            return
        key, accepted = QInputDialog.getText(self, "Nuevo recuerdo", "Clave corta:")
        if not accepted or not key.strip():
            return
        value, accepted = QInputDialog.getMultiLineText(self, "Nuevo recuerdo", "Contenido:")
        if not accepted or not value.strip():
            return
        result = remember(key, value, category, importance=0.7)
        if result.startswith("Sensitive"):
            QMessageBox.warning(self, "Memoria protegida", result)
        self.refresh()

    def edit_selected(self) -> None:
        entry = self._selected_entry()
        if not entry:
            return
        value, accepted = QInputDialog.getMultiLineText(
            self,
            "Editar recuerdo",
            f"{entry['category']}/{entry['key']}:",
            entry["value"],
        )
        if accepted and value.strip():
            remember(
                entry["key"],
                value,
                entry["category"],
                importance=float(entry["importance"]),
            )
            self.refresh()

    def delete_selected(self) -> None:
        entry = self._selected_entry()
        if not entry:
            return
        answer = QMessageBox.question(
            self,
            "Borrar recuerdo",
            f"¿Borrar {entry['category']}/{entry['key']}?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            delete_memory(memory_id=int(entry["id"]))
            self.refresh()

    def clear_all(self) -> None:
        answer = QMessageBox.warning(
            self,
            "Borrar toda la memoria",
            "Esta acción borrará todos los recuerdos de Jarvis. ¿Continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            clear_memories()
            self.refresh()


class VisualSettingsPanel(QFrame):
    changed = pyqtSignal(object)
    audio_devices_changed = pyqtSignal(object, object)
    history_requested = pyqtSignal()
    memory_requested = pyqtSignal()

    def __init__(self, settings: VisualSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setObjectName("visualSettings")
        self.setStyleSheet(
            """
            QFrame#visualSettings {
                background: rgba(13, 9, 11, 242);
                border: 1px solid rgba(255, 170, 45, 110);
                border-radius: 12px;
            }
            QLabel { color: rgba(244, 220, 224, 215); background: transparent; }
            QSlider::groove:horizontal {
                height: 3px; background: rgba(255, 255, 255, 32); border-radius: 1px;
            }
            QSlider::handle:horizontal {
                width: 13px; margin: -5px 0; border-radius: 6px;
                background: #e89a24;
            }
            QComboBox {
                color: #f6e3e6; background: rgba(255,255,255,18);
                border: 1px solid rgba(232,154,36,105); border-radius: 7px;
                padding: 4px 8px;
            }
            QCheckBox { color: rgba(244, 220, 224, 215); spacing: 7px; }
            QPushButton#historyButton {
                color: #f6e3e6; background: rgba(255,255,255,12);
                border: 1px solid rgba(232,154,36,105); border-radius: 7px;
                padding: 3px 8px;
            }
            QPushButton#historyButton:hover { background: rgba(232,154,36,38); }
            """
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 11)
        root.setSpacing(7)

        self.motion = self._slider_row(
            root, "Movimiento", 25, 160, int(settings.motion_intensity * 100), "%"
        )
        self.sensitivity = self._slider_row(
            root, "Sensibilidad", 40, 250, int(settings.microphone_sensitivity * 100), "%"
        )
        self.visibility = self._slider_row(
            root,
            "Visibilidad",
            int(MIN_VISIBILITY * 100),
            int(MAX_VISIBILITY * 100),
            int(settings.visibility * 100),
            "%",
        )
        self.size = self._slider_row(
            root,
            "Tamaño",
            MIN_ASSISTANT_SIZE,
            MAX_ASSISTANT_SIZE,
            settings.assistant_size,
            " px",
            estimated_core_diameter,
        )

        selectors = QHBoxLayout()
        selectors.setSpacing(8)
        selectors.addWidget(self._small_label("Calidad"))
        self.quality_combo = QComboBox()
        self.quality_combo.addItem("Ahorro", "economy")
        self.quality_combo.addItem("Equilibrada", "balanced")
        self.quality_combo.addItem("Alta", "high")
        quality_index = self.quality_combo.findData(settings.quality)
        self.quality_combo.setCurrentIndex(max(0, quality_index))
        selectors.addWidget(self.quality_combo, 1)
        self.history_button = QPushButton("HISTORIAL")
        self.history_button.setObjectName("historyButton")
        self.history_button.setFixedHeight(24)
        self.history_button.setToolTip("Ver conversaciones, respuestas y errores")
        selectors.addWidget(self.history_button)
        self.memory_button = QPushButton("MEMORIA")
        self.memory_button.setObjectName("historyButton")
        self.memory_button.setFixedHeight(24)
        self.memory_button.setToolTip("Ver, buscar, editar y borrar recuerdos")
        selectors.addWidget(self.memory_button)
        root.addLayout(selectors)

        self.input_device_combo = self._audio_device_row(root, "Entrada de audio")
        self.output_device_combo = self._audio_device_row(root, "Salida de audio")
        self.refresh_audio_devices()

        switches = QHBoxLayout()
        self.computer_control = QCheckBox("Control del PC")
        self.computer_control.setChecked(settings.computer_control_enabled)
        self.computer_control.setToolTip(
            "Permite abrir y manejar aplicaciones con límites de seguridad"
        )
        self.reduced = QCheckBox("Reducir movimiento")
        self.reduced.setChecked(settings.reduced_motion)
        switches.addWidget(self.computer_control)
        switches.addStretch()
        switches.addWidget(self.reduced)
        root.addLayout(switches)

        self.motion.valueChanged.connect(self._publish)
        self.sensitivity.valueChanged.connect(self._publish)
        self.visibility.valueChanged.connect(self._publish)
        self.size.valueChanged.connect(self._publish)
        self.quality_combo.currentIndexChanged.connect(self._publish)
        self.input_device_combo.currentIndexChanged.connect(self._publish_audio_devices)
        self.output_device_combo.currentIndexChanged.connect(self._publish_audio_devices)
        self.history_button.clicked.connect(self.history_requested.emit)
        self.memory_button.clicked.connect(self.memory_requested.emit)
        self.computer_control.toggled.connect(self._publish)
        self.reduced.toggled.connect(self._publish)

    @staticmethod
    def _small_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setFont(QFont("Segoe UI", 8))
        return label

    def _slider_row(
        self,
        parent_layout: QVBoxLayout,
        text: str,
        minimum: int,
        maximum: int,
        value: int,
        suffix: str = "",
        display_value=None,
    ) -> QSlider:
        row = QHBoxLayout()
        label = self._small_label(text)
        label.setFixedWidth(82)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(value)
        row.addWidget(label)
        row.addWidget(slider, 1)
        readout = self._small_label("")
        readout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        readout.setFixedWidth(48)
        formatter = display_value or (lambda current: current)
        update_readout = lambda current: readout.setText(f"{formatter(current)}{suffix}")
        slider.valueChanged.connect(update_readout)
        update_readout(value)
        row.addWidget(readout)
        parent_layout.addLayout(row)
        return slider

    def _audio_device_row(self, parent_layout: QVBoxLayout, text: str) -> QComboBox:
        row = QHBoxLayout()
        label = self._small_label(text)
        label.setFixedWidth(82)
        combo = QComboBox()
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(24)
        combo.setToolTip(f"Seleccionar {text.lower()}")
        row.addWidget(label)
        row.addWidget(combo, 1)
        parent_layout.addLayout(row)
        return combo

    @staticmethod
    def _fill_audio_combo(
        combo: QComboBox,
        devices: list[tuple[str, int]],
        selected_device: int | None,
    ) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Predeterminado del sistema", None)
        for label, device_id in devices:
            combo.addItem(label, device_id)
        selected_index = combo.findData(selected_device)
        combo.setCurrentIndex(max(0, selected_index))
        combo.blockSignals(False)

    def refresh_audio_devices(self) -> None:
        current_input_label = (
            self.input_device_combo.currentText()
            if self.input_device_combo.count()
            else get_secret("INPUT_DEVICE_NAME")
        )
        current_output_label = (
            self.output_device_combo.currentText()
            if self.output_device_combo.count()
            else get_secret("OUTPUT_DEVICE_NAME")
        )
        current_input = (
            self.input_device_combo.currentData()
            if self.input_device_combo.count()
            else _device_id_from_setting("INPUT_DEVICE")
        )
        current_output = (
            self.output_device_combo.currentData()
            if self.output_device_combo.count()
            else _device_id_from_setting("OUTPUT_DEVICE")
        )
        try:
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
            inputs = enumerate_audio_devices("input", devices, hostapis)
            outputs = enumerate_audio_devices("output", devices, hostapis)
        except Exception:
            return
        resolved_input = _canonical_audio_device(
            current_input, devices, inputs, current_input_label
        )
        resolved_output = _canonical_audio_device(
            current_output, devices, outputs, current_output_label
        )
        self._fill_audio_combo(self.input_device_combo, inputs, resolved_input)
        self._fill_audio_combo(self.output_device_combo, outputs, resolved_output)
        resolved_input_name = (
            self.input_device_combo.currentText() if resolved_input is not None else ""
        )
        resolved_output_name = (
            self.output_device_combo.currentText() if resolved_output is not None else ""
        )
        write_audio_devices(
            resolved_input,
            resolved_output,
            resolved_input_name,
            resolved_output_name,
        )
        if (resolved_input, resolved_output) != (current_input, current_output):
            self.audio_devices_changed.emit(resolved_input, resolved_output)

    def _publish_audio_devices(self, *_args) -> None:
        input_device = self.input_device_combo.currentData()
        output_device = self.output_device_combo.currentData()
        input_name = self.input_device_combo.currentText() if input_device is not None else ""
        output_name = self.output_device_combo.currentText() if output_device is not None else ""
        write_audio_devices(input_device, output_device, input_name, output_name)
        self.audio_devices_changed.emit(input_device, output_device)

    def _publish(self, *_args) -> None:
        self.settings.motion_intensity = self.motion.value() / 100.0
        self.settings.microphone_sensitivity = self.sensitivity.value() / 100.0
        self.settings.visibility = self.visibility.value() / 100.0
        self.settings.assistant_size = self.size.value()
        self.settings.quality = str(self.quality_combo.currentData())
        self.settings.droplets = True
        self.settings.reduced_motion = self.reduced.isChecked()
        self.settings.computer_control_enabled = self.computer_control.isChecked()
        self.changed.emit(self.settings.validate())


class LiquidMainWindow(QMainWindow):
    state_signal = pyqtSignal(str)
    log_signal = pyqtSignal(str)
    audio_refresh_signal = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("JARVIS")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

        self.on_text_command = None
        self.on_manual_activate = None
        self.on_audio_devices_changed = None
        self.on_audio_refresh_requested = None
        self._muted = False
        self._current_file: str | None = None
        self._ready = is_configured()
        self._state = AssistantState.IDLE
        self._panel_visible = False
        self._settings_visible = False
        self._last_log = ""
        self._history_dialog: HistoryDialog | None = None
        self._memory_dialog: MemoryDialog | None = None
        self._last_audio_refresh_request = 0.0
        self._last_system_audio_signature = None
        self.settings = load_visual_settings()
        self.analyzer = AudioReactiveAnalyzer(
            sensitivity=self.settings.microphone_sensitivity
        )

        central = QWidget()
        central.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        central.setStyleSheet("background: transparent;")
        self.setCentralWidget(central)
        self.root_layout = QVBoxLayout(central)
        self.root_layout.setContentsMargins(0, 0, 0, 0)
        self.root_layout.setSpacing(7)
        self.root_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

        self.renderer = LiquidGoldRenderer(self.analyzer, self.settings)
        self.renderer.clicked.connect(self.toggle_panel)
        self.renderer.context_requested.connect(self._show_context_menu)
        self.renderer.shader_failed.connect(self._on_shader_failure)
        self.root_layout.addWidget(self.renderer, 0, Qt.AlignmentFlag.AlignHCenter)

        self.control_panel = self._build_control_panel()
        self.control_panel.hide()
        self.root_layout.addWidget(self.control_panel, 0, Qt.AlignmentFlag.AlignHCenter)

        self.settings_panel = VisualSettingsPanel(self.settings)
        self.settings_panel.changed.connect(self._apply_visual_settings)
        self.settings_panel.audio_devices_changed.connect(self._audio_devices_selected)
        self.settings_panel.history_requested.connect(self.show_history)
        self.settings_panel.memory_requested.connect(self.show_memory)
        self.settings_panel.hide()
        self.root_layout.addWidget(self.settings_panel, 0, Qt.AlignmentFlag.AlignHCenter)

        self._panel_opacity = QGraphicsOpacityEffect(self.control_panel)
        self.control_panel.setGraphicsEffect(self._panel_opacity)
        self._panel_animation = QPropertyAnimation(self._panel_opacity, b"opacity", self)
        self._panel_animation.setDuration(150)
        self._panel_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._auto_hide = QTimer(self)
        self._auto_hide.setSingleShot(True)
        self._auto_hide.setInterval(7500)
        self._auto_hide.timeout.connect(self._auto_hide_panel)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(350)
        self._save_timer.timeout.connect(lambda: save_visual_settings(self.settings))
        self._audio_change_debounce = QTimer(self)
        self._audio_change_debounce.setSingleShot(True)
        self._audio_change_debounce.setInterval(350)
        self._audio_change_debounce.timeout.connect(self.request_audio_backend_refresh)
        self._audio_watch_timer = QTimer(self)
        self._audio_watch_timer.setInterval(3000)
        self._audio_watch_timer.timeout.connect(self._poll_system_audio_devices)
        self._media_devices = None
        if QMediaDevices is not None:
            try:
                self._media_devices = QMediaDevices(self)
                self._media_devices.audioInputsChanged.connect(
                    self._audio_change_debounce.start
                )
                self._media_devices.audioOutputsChanged.connect(
                    self._audio_change_debounce.start
                )
                self._last_system_audio_signature = self._system_audio_signature()
                self._audio_watch_timer.start()
            except Exception:
                self._media_devices = None

        self.state_signal.connect(self._apply_state)
        self.log_signal.connect(self._receive_log)
        self.audio_refresh_signal.connect(self.settings_panel.refresh_audio_devices)
        self._configure_shortcuts()
        self._apply_visual_settings(self.settings, save=False)
        QTimer.singleShot(0, self._center_on_screen)
        if not self._ready:
            QTimer.singleShot(0, lambda: self.show_panel(force=True))

    def _build_control_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("floatingControls")
        panel.setStyleSheet(
            """
            QFrame#floatingControls {
                background: rgba(12, 8, 10, 239);
                border: 1px solid rgba(255, 170, 45, 115);
                border-radius: 15px;
            }
            QLabel { color: rgba(244, 220, 224, 218); background: transparent; }
            QLineEdit {
                color: #fff0f2; background: rgba(255,255,255,17);
                border: 1px solid rgba(232,154,36,92); border-radius: 10px;
                padding: 7px 10px; selection-background-color: #9a5a0d;
            }
            QLineEdit:focus { border: 1px solid rgba(255,187,74,205); }
            QPushButton {
                color: #f7e7e9; background: rgba(255,255,255,14);
                border: 1px solid rgba(232,154,36,88); border-radius: 9px;
                padding: 5px 9px;
            }
            QPushButton:hover { background: rgba(232,154,36,42); }
            QPushButton:pressed { background: rgba(232,154,36,68); }
            """
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 9, 12, 11)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self.state_label = QLabel("JARVIS // EN REPOSO")
        self.state_label.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        header.addWidget(self.state_label)
        header.addStretch()

        self.settings_button = self._icon_button("AJUSTES", "Configuración visual")
        self.settings_button.clicked.connect(self.toggle_settings)
        self.minimize_button = self._icon_button("—", "Minimizar")
        self.minimize_button.clicked.connect(self.showMinimized)
        self.close_button = self._icon_button("×", "Cerrar Jarvis")
        self.close_button.clicked.connect(self.close)
        header.addWidget(self.settings_button)
        header.addWidget(self.minimize_button)
        header.addWidget(self.close_button)
        layout.addLayout(header)

        command_row = QHBoxLayout()
        command_row.setSpacing(7)
        self.input = QLineEdit()
        self.input.setFont(QFont("Segoe UI", 9))
        self.input.setPlaceholderText(
            "Pega tu clave de Gemini" if not self._ready else "Escribe una orden…"
        )
        self.input.setEchoMode(
            QLineEdit.EchoMode.Normal if self._ready else QLineEdit.EchoMode.Password
        )
        self.input.returnPressed.connect(self._submit_or_activate)
        self.input.textChanged.connect(self._input_changed)
        self.input.textEdited.connect(lambda: self._arm_auto_hide())
        command_row.addWidget(self.input, 1)

        self.api_link_button = QPushButton("OBTENER CLAVE")
        self.api_link_button.setToolTip("Abrir Google AI Studio")
        self.api_link_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://aistudio.google.com/apikey"))
        )
        self.api_link_button.setVisible(not self._ready)
        command_row.addWidget(self.api_link_button)

        self.activate_button = QPushButton("ACTIVAR" if self._ready else "GUARDAR")
        self.activate_button.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        self.activate_button.clicked.connect(self._submit_or_activate)
        command_row.addWidget(self.activate_button)

        self.mic_button = QPushButton("MIC")
        self.mic_button.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        self.mic_button.clicked.connect(self._toggle_mute)
        self.mic_button.setEnabled(self._ready)
        self.mic_button.setVisible(self._ready)
        command_row.addWidget(self.mic_button)
        layout.addLayout(command_row)
        return panel

    @staticmethod
    def _popup_style() -> str:
        return """
            QMenu {
                color: #fff0d7;
                background: rgba(18, 12, 5, 248);
                border: 1px solid rgba(255, 178, 62, 145);
                border-radius: 9px;
                padding: 5px;
            }
            QMenu::item {
                padding: 8px 28px 8px 12px;
                border-radius: 6px;
            }
            QMenu::item:selected { background: rgba(232, 154, 36, 60); }
            QMenu::separator {
                height: 1px; background: rgba(255,178,62,55); margin: 4px 7px;
            }
            QLineEdit {
                color: #fff5e4; background: rgba(255,255,255,18);
                border: 1px solid rgba(255,178,62,105); border-radius: 8px;
                padding: 7px 9px; selection-background-color: #9a5a0d;
            }
            QLineEdit:focus { border: 1px solid rgba(255,190,82,215); }
            QPushButton {
                color: #fff1dc; background: rgba(232,154,36,42);
                border: 1px solid rgba(255,178,62,115); border-radius: 8px;
                padding: 7px 10px;
            }
            QPushButton:hover { background: rgba(232,154,36,72); }
        """

    @staticmethod
    def _icon_button(text: str, tooltip: str) -> QPushButton:
        button = QPushButton(text)
        button.setToolTip(tooltip)
        button.setFixedHeight(27)
        if text != "AJUSTES":
            button.setFixedWidth(30)
        return button

    def _configure_shortcuts(self) -> None:
        mute_shortcut = QShortcut(QKeySequence("F4"), self)
        mute_shortcut.activated.connect(self._toggle_mute)
        panel_shortcut = QShortcut(QKeySequence("F11"), self)
        panel_shortcut.activated.connect(self.toggle_panel)
        escape_shortcut = QShortcut(QKeySequence("Escape"), self)
        escape_shortcut.activated.connect(self.hide_panel)

    def _input_changed(self, text: str) -> None:
        if not self._ready:
            self.activate_button.setText("GUARDAR")
        else:
            self.activate_button.setText("ENVIAR" if text.strip() else "ACTIVAR")

    def _submit_or_activate(self) -> None:
        text = self.input.text().strip()
        if not self._ready:
            if not text:
                self.input.setStyleSheet("border: 1px solid rgba(255,176,56,220);")
                return
            write_env(text, "")
            self._ready = True
            self.input.clear()
            self.input.setEchoMode(QLineEdit.EchoMode.Normal)
            self.input.setPlaceholderText("Escribe una orden…")
            self.activate_button.setText("ACTIVAR")
            self.mic_button.setEnabled(True)
            self.mic_button.show()
            self.api_link_button.hide()
            self._apply_state(AssistantState.IDLE.value)
            self._arm_auto_hide()
            return
        if text:
            self.input.clear()
            self._dispatch_text_command(text)
        elif self.on_manual_activate:
            self.on_manual_activate()
        self._arm_auto_hide()

    def _dispatch_text_command(self, text: str) -> None:
        if self.on_text_command:
            threading.Thread(
                target=self.on_text_command, args=(text,), daemon=True
            ).start()

    def _show_context_menu(self, global_pos) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(self._popup_style())
        close_action = menu.addAction("Cerrar Jarvis")
        selected = menu.exec(global_pos)
        if selected is close_action:
            self.close()

    def _toggle_mute(self) -> None:
        if not self._ready:
            return
        self._muted = not self._muted
        self._apply_state("MUTED" if self._muted else "LISTENING")

    def _style_mic(self) -> None:
        if self._muted:
            self.mic_button.setText("MUTED")
            self.mic_button.setStyleSheet(
                "color:#927b7f; background:rgba(66,36,41,85);"
                "border:1px solid rgba(142,71,81,90); border-radius:9px;"
            )
        else:
            self.mic_button.setText("MIC")
            self.mic_button.setStyleSheet("")

    def _apply_state(self, raw_state: str) -> None:
        state = normalize_state(raw_state)
        if self._muted:
            state = AssistantState.DISABLED
        self._state = state
        self.renderer.set_state(state)
        self.state_label.setText(f"JARVIS // {state_label(state)}")
        self._style_mic()
        if state in {
            AssistantState.LISTENING,
            AssistantState.THINKING,
            AssistantState.SPEAKING,
            AssistantState.ERROR,
        }:
            self.raise_()

    def _receive_log(self, text: str) -> None:
        self._last_log = str(text)
        append_history(self._last_log)
        if self._last_log.startswith("ERR:"):
            self._apply_state(AssistantState.ERROR.value)

    @staticmethod
    def _system_audio_signature():
        if QMediaDevices is None:
            return None
        try:
            inputs = tuple(
                sorted((bytes(item.id()), item.description()) for item in QMediaDevices.audioInputs())
            )
            outputs = tuple(
                sorted((bytes(item.id()), item.description()) for item in QMediaDevices.audioOutputs())
            )
            return inputs, outputs
        except Exception:
            return None

    def _poll_system_audio_devices(self) -> None:
        signature = self._system_audio_signature()
        if signature is None:
            return
        if self._last_system_audio_signature is None:
            self._last_system_audio_signature = signature
            return
        if signature != self._last_system_audio_signature:
            self._last_system_audio_signature = signature
            self._audio_change_debounce.start()

    def request_audio_backend_refresh(self) -> None:
        now = time.monotonic()
        if now - self._last_audio_refresh_request < 0.75:
            return
        self._last_audio_refresh_request = now
        if self.on_audio_refresh_requested:
            self.on_audio_refresh_requested()
        else:
            self.settings_panel.refresh_audio_devices()

    def refresh_audio_devices(self) -> None:
        self.audio_refresh_signal.emit()

    def show_history(self) -> None:
        if self._history_dialog is None:
            self._history_dialog = HistoryDialog(self)
        self._history_dialog.refresh()
        self._history_dialog.show()
        self._history_dialog.raise_()
        self._history_dialog.activateWindow()

    def show_memory(self) -> None:
        if self._memory_dialog is None:
            self._memory_dialog = MemoryDialog(self)
        self._memory_dialog.refresh()
        self._memory_dialog.show()
        self._memory_dialog.raise_()
        self._memory_dialog.activateWindow()

    def _on_shader_failure(self, error: str) -> None:
        self._last_log = f"Renderizador simplificado: {error}"

    def toggle_panel(self) -> None:
        if self._panel_visible:
            self.hide_panel()
        else:
            self.show_panel()

    def show_panel(self, force: bool = False) -> None:
        if self._panel_visible:
            self._arm_auto_hide()
            return
        self._panel_visible = True
        self.control_panel.show()
        self._panel_opacity.setOpacity(0.0)
        self._resize_for_contents()
        self._panel_animation.stop()
        self._panel_animation.setStartValue(0.0)
        self._panel_animation.setEndValue(1.0)
        self._panel_animation.start()
        if self._ready and not force:
            self._arm_auto_hide()
        else:
            self._auto_hide.stop()

    def hide_panel(self) -> None:
        if not self._panel_visible or not self._ready:
            return
        self._auto_hide.stop()
        self._panel_animation.stop()
        self.control_panel.hide()
        self.settings_panel.hide()
        self._settings_visible = False
        self._panel_visible = False
        self._resize_for_contents()

    def _auto_hide_panel(self) -> None:
        if self.input.hasFocus() or self._settings_visible:
            self._arm_auto_hide()
            return
        self.hide_panel()

    def _arm_auto_hide(self) -> None:
        if self._ready and self._panel_visible:
            self._auto_hide.start()

    def toggle_settings(self) -> None:
        self._settings_visible = not self._settings_visible
        if self._settings_visible:
            self.settings_panel.refresh_audio_devices()
            self.request_audio_backend_refresh()
        self.settings_panel.setVisible(self._settings_visible)
        self._resize_for_contents()
        self._arm_auto_hide()

    def _audio_devices_selected(self, input_device, output_device) -> None:
        if self.on_audio_devices_changed:
            self.on_audio_devices_changed(input_device, output_device)

    def _apply_visual_settings(
        self, settings: VisualSettings, save: bool = True
    ) -> None:
        self.settings = settings.validate()
        self.renderer.apply_settings(self.settings)
        extent = self.settings.assistant_size + RENDERER_PADDING
        self.renderer.setFixedSize(extent, extent)
        panel_width = max(410, min(486, extent))
        self.control_panel.setFixedWidth(panel_width)
        self.settings_panel.setFixedWidth(panel_width)
        self._resize_for_contents()
        if save:
            self._save_timer.start()

    def _resize_for_contents(self) -> None:
        extent = self.settings.assistant_size + RENDERER_PADDING
        panel_height = self.control_panel.sizeHint().height() + 7 if self._panel_visible else 0
        settings_height = self.settings_panel.sizeHint().height() + 7 if self._settings_visible else 0
        width = max(extent, self.control_panel.width())
        height = extent + panel_height + settings_height
        self.setFixedSize(width, height)
        QTimer.singleShot(0, self._update_interaction_mask)
        screen = self.screen().availableGeometry() if self.screen() else QApplication.primaryScreen().availableGeometry()
        if self.frameGeometry().bottom() > screen.bottom():
            self.move(self.x(), max(screen.top(), screen.bottom() - self.height()))

    def _update_interaction_mask(self) -> None:
        renderer_geometry = self.renderer.geometry().adjusted(18, 18, -18, -18)
        path = QPainterPath()
        path.addEllipse(QRectF(renderer_geometry))
        if self._panel_visible:
            path.addRoundedRect(QRectF(self.control_panel.geometry()), 15.0, 15.0)
        if self._settings_visible:
            path.addRoundedRect(QRectF(self.settings_panel.geometry()), 12.0, 12.0)
        polygon = path.toFillPolygon().toPolygon()
        self.setMask(QRegion(polygon))

    def _center_on_screen(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            screen.x() + (screen.width() - self.width()) // 2,
            screen.y() + (screen.height() - self.height()) // 2,
        )
        self._update_interaction_mask()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._auto_hide.stop()
        self._save_timer.stop()
        save_visual_settings(self.settings)
        self.analyzer.reset()
        super().closeEvent(event)


class LiquidJarvisUI:
    """Compatibility facade consumed by the unchanged assistant runtime."""

    def __init__(self, face_path: str = "", size=None):
        del face_path, size
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._app.setQuitOnLastWindowClosed(True)
        self._win = LiquidMainWindow()
        self._win.show()
        self.root = _RootShim(self._app)

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, value: bool) -> None:
        value = bool(value)
        if value != self._win._muted:
            self._win._muted = value
            self._win.state_signal.emit("MUTED" if value else "LISTENING")

    @property
    def current_file(self) -> str | None:
        return self._win._current_file

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, callback) -> None:
        self._win.on_text_command = callback

    @property
    def on_manual_activate(self):
        return self._win.on_manual_activate

    @on_manual_activate.setter
    def on_manual_activate(self, callback) -> None:
        self._win.on_manual_activate = callback

    @property
    def on_audio_devices_changed(self):
        return self._win.on_audio_devices_changed

    @on_audio_devices_changed.setter
    def on_audio_devices_changed(self, callback) -> None:
        self._win.on_audio_devices_changed = callback

    @property
    def on_audio_refresh_requested(self):
        return self._win.on_audio_refresh_requested

    @on_audio_refresh_requested.setter
    def on_audio_refresh_requested(self, callback) -> None:
        self._win.on_audio_refresh_requested = callback
        if callback is not None:
            QTimer.singleShot(0, self._win.request_audio_backend_refresh)

    def refresh_audio_devices(self) -> None:
        try:
            self._win.audio_refresh_signal.emit()
        except RuntimeError:
            return

    def set_state(self, state: str) -> None:
        try:
            self._win.state_signal.emit(str(state))
        except RuntimeError:
            # The daemon audio loop may finish one last callback while Qt is
            # tearing the native window down during application shutdown.
            return

    def write_log(self, text: str) -> None:
        try:
            self._win.log_signal.emit(str(text))
        except RuntimeError:
            return

    def feed_input_audio(self, pcm: bytes, sample_rate: int) -> None:
        try:
            self._win.analyzer.feed_input(pcm, sample_rate)
        except RuntimeError:
            return

    def feed_output_audio(self, pcm: bytes, sample_rate: int) -> None:
        try:
            self._win.analyzer.feed_output(pcm, sample_rate)
        except RuntimeError:
            return

    def wait_for_api_key(self) -> None:
        while not self._win._ready:
            time.sleep(0.1)

    def start_speaking(self) -> None:
        self.set_state("SPEAKING")

    def stop_speaking(self) -> None:
        if not self.muted:
            self.set_state("LISTENING")
