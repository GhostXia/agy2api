"""واجهة مكتبية سريعة لتفريغ الملفات الصوتية عبر agy2api.

تستقبل مسار الملف الصوتي وتمرره للوكيل عبر صيغة @path للاستنطاق والتفريغ الكامل.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

DEFAULT_API_URL = "http://127.0.0.1:7862/v1/chat/completions"
DEFAULT_API_KEY = os.getenv("AGY2API_KEY", os.getenv("API_KEY", "pwd"))


class TranscribeWorker(QThread):
    """خيط عمل خلفي لإرسال طلب التفريغ دون تجميد الواجهة."""

    transcription_ready = Signal(str)
    error_occurred = Signal(str)

    def __init__(
        self,
        api_url: str,
        file_path: str,
        api_key: str = DEFAULT_API_KEY,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.api_url = api_url
        self.file_path = file_path
        self.api_key = api_key

    def run(self) -> None:
        """إرسال مسار الملف الصوتي مسبوقاً بـ @ إلى خادم agy2api."""
        prompt = (
            "استخدم أداة read_file فقط لقراءة واستماع هذا الملف الصوتي "
            f"@{self.file_path} وفرغ نصه المنطوق بالكامل بدقة تامة مع ضبط "
            "الآيات والعبارات. يُحظر حظراً باتاً تشغيل أوامر النظام أو "
            "استخدام أي نموذج أو سكربت محلي."
        )
        payload = json.dumps(
            {
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        req = urllib.request.Request(
            self.api_url,
            data=payload,
            headers=headers,
        )

        try:
            # مهلة انتظار سخية للملفات الصوتية الطويلة
            with urllib.request.urlopen(req, timeout=600) as response:
                result = json.loads(response.read().decode("utf-8"))
                content = result["choices"][0]["message"]["content"]
                self.transcription_ready.emit(content)
        except urllib.error.URLError as exc:
            self.error_occurred.emit(
                f"تعذر الاتصال بالخادم ({exc}). تأكد من تشغيل agy2api أولاً."
            )
        except Exception as exc:  # noqa: BLE001
            self.error_occurred.emit(f"حدث خطأ أثناء التفريغ: {exc}")


class TranscribeWindow(QMainWindow):
    """نافذة التفريغ الصوتي المكتبي المتوافقة مع قارئات الشاشة."""

    def __init__(self) -> None:
        super().__init__()
        self.worker: TranscribeWorker | None = None
        self._init_ui()

    def _init_ui(self) -> None:
        self.setWindowTitle("مُفرِّغ الصوتيات - agy2api")
        self.resize(650, 500)

        main_widget = QWidget(self)
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)
        layout.setSpacing(10)

        # 1. شريط انتقاء الملف
        file_layout = QHBoxLayout()
        self.btn_browse = QPushButton("استعراض ملف صوتي...", self)
        self.btn_browse.setAccessibleName("استعراض لاختيار ملف صوتي من القرص")
        self.btn_browse.clicked.connect(self._choose_file)
        file_layout.addWidget(self.btn_browse)

        self.edit_file_path = QLineEdit(self)
        self.edit_file_path.setPlaceholderText("لم يتم اختيار ملف صوتي بعد")
        self.edit_file_path.setAccessibleName("مسار الملف الصوتي المختار")
        self.edit_file_path.textChanged.connect(self._on_path_changed)
        file_layout.addWidget(self.edit_file_path)

        layout.addLayout(file_layout)

        # 2. زر بدء التفريغ
        self.btn_transcribe = QPushButton("بدء التفريغ عبر الوكيل", self)
        self.btn_transcribe.setAccessibleName(
            "بدء تفريغ الملف الصوتي المحدد عبر الوكيل agy"
        )
        self.btn_transcribe.setEnabled(False)
        self.btn_transcribe.clicked.connect(self._start_transcription)
        layout.addWidget(self.btn_transcribe)

        # 3. شريط الحالة
        self.label_status = QLabel("جاهز للاستخدام.", self)
        self.label_status.setAccessibleName("حالة العملية الحالية")
        layout.addWidget(self.label_status)

        # 4. صندوق النص المفرغ
        self.text_result = QPlainTextEdit(self)
        self.text_result.setPlaceholderText("سيظهر النص المفرغ هنا...")
        self.text_result.setAccessibleName("صندوق عرض النص المفرغ")
        layout.addWidget(self.text_result)

        # 5. أزرار التعامل مع النتيجة (نسخ / حفظ)
        action_layout = QHBoxLayout()
        self.btn_copy = QPushButton("نسخ النص للحافظة", self)
        self.btn_copy.setAccessibleName("نسخ كامل النص المفرغ إلى الحافظة")
        self.btn_copy.setEnabled(False)
        self.btn_copy.clicked.connect(self._copy_text)
        action_layout.addWidget(self.btn_copy)

        self.btn_save = QPushButton("حفظ في ملف نصي...", self)
        self.btn_save.setAccessibleName("حفظ النص المفرغ في ملف نصي على القرص")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._save_file)
        action_layout.addWidget(self.btn_save)

        layout.addLayout(action_layout)

        # ضبط تسلسل التنقل لقارئ الشاشة (Tab Order)
        QWidget.setTabOrder(self.btn_browse, self.edit_file_path)
        QWidget.setTabOrder(self.edit_file_path, self.btn_transcribe)
        QWidget.setTabOrder(self.btn_transcribe, self.text_result)
        QWidget.setTabOrder(self.text_result, self.btn_copy)
        QWidget.setTabOrder(self.btn_copy, self.btn_save)

    def _choose_file(self) -> None:
        filters = (
            "ملفات صوتية (*.mp3 *.wav *.m4a *.ogg *.webm *.flac *.aac);;"
            "جميع الملفات (*.*)"
        )
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "اختر ملفاً صوتياً للتفريغ",
            "",
            filters,
        )
        if file_path:
            self.edit_file_path.setText(file_path)

    def _on_path_changed(self, text: str) -> None:
        has_file = bool(text.strip() and Path(text.strip()).exists())
        self.btn_transcribe.setEnabled(has_file)
        if has_file:
            self.label_status.setText("تم تحديد الملف، اضغط زر التفريغ للبدء.")

    def _start_transcription(self) -> None:
        file_path = self.edit_file_path.text().strip()
        if not file_path:
            return

        self._set_busy(True)
        self.label_status.setText("جارٍ التفريغ عبر الوكيل، انتظر لحظات...")

        self.worker = TranscribeWorker(
            DEFAULT_API_URL,
            file_path,
            api_key=DEFAULT_API_KEY,
            parent=self,
        )
        self.worker.transcription_ready.connect(self._on_success)
        self.worker.error_occurred.connect(self._on_error)
        self.worker.start()

    def _on_success(self, text: str) -> None:
        self.text_result.setPlainText(text)
        self.label_status.setText("اكتمل التفريغ بنجاح.")
        self._set_busy(False)
        self.btn_copy.setEnabled(bool(text))
        self.btn_save.setEnabled(bool(text))

    def _on_error(self, err_msg: str) -> None:
        self.label_status.setText("تعذر إتمام التفريغ.")
        self._set_busy(False)
        QMessageBox.critical(self, "خطأ في التفريغ", err_msg)

    def _set_busy(self, busy: bool) -> None:
        self.btn_browse.setEnabled(not busy)
        self.edit_file_path.setEnabled(not busy)
        self.btn_transcribe.setEnabled(not busy)

    def _copy_text(self) -> None:
        text = self.text_result.toPlainText()
        if text:
            clipboard = QApplication.clipboard()
            clipboard.setText(text)
            self.label_status.setText("نُسخ النص إلى الحافظة.")

    def _save_file(self) -> None:
        text = self.text_result.toPlainText()
        if not text:
            return

        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "حفظ النص المفرغ",
            "transcription.txt",
            "ملفات نصية (*.txt);;جميع الملفات (*.*)",
        )
        if out_path:
            Path(out_path).write_text(text, encoding="utf-8")
            self.label_status.setText(f"حُفظ الملف في: {Path(out_path).name}")


def main() -> None:
    """نقطة تشغيل تطبيق التفريغ."""
    app = QApplication(sys.argv)
    window = TranscribeWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
