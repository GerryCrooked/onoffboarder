import os
from io import BytesIO
from flask import render_template, current_app # current_app für den Zugriff auf app.static_folder
from werkzeug.utils import secure_filename
import logging

try:
    from weasyprint import HTML, CSS # CSS importieren
    from pypdf import PdfWriter, PdfReader
except ImportError:
    HTML = CSS = PdfWriter = PdfReader = None

logger = logging.getLogger(__name__)

def create_combined_pdf(request_item, concrete_upload_folder, base_url_for_html_assets, pdf_template_name="view_for_pdf.html"):
    if HTML is None or PdfWriter is None or PdfReader is None or CSS is None:
        logger.error("WeasyPrint, pypdf oder CSS-Objekt nicht im pdf_generator verfügbar.")
        raise ImportError("PDF-Erstellungsbibliotheken nicht verfügbar.")

    pdf_merger = PdfWriter()

    try:
        html_string = render_template(
            pdf_template_name,
            request=request_item
            # get_hardware_details_for_display ist global verfügbar
        )

        # CSS explizit laden
        stylesheets = []
        # Der Pfad zu static_folder wird relativ zum app-Root erwartet.
        # current_app.static_folder gibt den absoluten Pfad zum static-Ordner der Flask-App.
        path_to_print_css = os.path.join(current_app.static_folder, 'css', 'print.css')

        if os.path.exists(path_to_print_css):
            stylesheets.append(CSS(filename=path_to_print_css))
            logger.info(f"Lade CSS explizit für PDF: {path_to_print_css}")
        else:
            logger.warning(f"print.css NICHT gefunden unter: {path_to_print_css}. PDF wird mit Standard-Browser-Stilen (falls im HTML) oder WeasyPrint-Defaults gerendert.")

        html_obj = HTML(string=html_string, base_url=base_url_for_html_assets)
        antrag_pdf_bytes = html_obj.write_pdf(stylesheets=stylesheets) # Stylesheets hier übergeben

        antrag_pdf_stream = BytesIO(antrag_pdf_bytes)
        pdf_merger.append(fileobj=antrag_pdf_stream)
        logger.info(f"Antrag {request_item.get('id')} als PDF (Teil 1) generiert.")

        # Anhänge hinzufügen (Logik bleibt gleich)
        attachments_to_merge = []
        if request_item.get('key_issuance_protocol_filename'):
            attachments_to_merge.append(request_item.get('key_issuance_protocol_filename'))

        for attachment_filename in attachments_to_merge:
            if attachment_filename:
                safe_attachment_filename = secure_filename(attachment_filename)
                file_path = os.path.join(concrete_upload_folder, safe_attachment_filename)
                if os.path.exists(file_path):
                    try:
                        with open(file_path, "rb") as f_attach:
                            attachment_pdf_reader = PdfReader(f_attach)
                            for page_num in range(len(attachment_pdf_reader.pages)):
                                pdf_merger.add_page(attachment_pdf_reader.pages[page_num])
                        logger.info(f"Anhang '{safe_attachment_filename}' zu PDF für Antrag {request_item.get('id')} hinzugefügt.")
                    except Exception as e_attach:
                        logger.error(f"Fehler beim Lesen/Hinzufügen des Anhangs '{safe_attachment_filename}': {e_attach}")
                else:
                    logger.warning(f"Anhang-Datei '{safe_attachment_filename}' nicht gefunden: '{file_path}'.")

        merged_pdf_stream = BytesIO()
        pdf_merger.write(merged_pdf_stream)
        merged_pdf_stream.seek(0)
        pdf_merger.close()

        logger.info(f"Kombiniertes PDF für Antrag {request_item.get('id')} erfolgreich erstellt.")
        return merged_pdf_stream

    except Exception as e:
        logger.error(f"Fehler in create_combined_pdf für Antrag {request_item.get('id')}: {e}", exc_info=True)
        raise
