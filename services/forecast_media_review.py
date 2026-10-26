"""Bounded LOCAL media assessment, not an OCR automatic-admission shortcut.

Native PDFs retain the existing strict unattended policy. Images/scanned PDFs
produce review candidates only, even when two Tesseract passes agree. Agreement
and confidence from one engine are evidence, not proof of source-cell accuracy.
No network, storage writes, scheduling, enrollment or messages occur here.
"""
from __future__ import annotations

from datetime import datetime
from io import BytesIO
import hashlib
import math
import re

from services.forecast_import import NUMERIC_FIELDS, normalize_forecast_rows, parse_forecast_text, validate_forecast
from services.whatsapp_forecast import (
    ForecastNeedsCorrection, ForecastOCRRequired, MAX_IMAGE_BYTES, MAX_PDF_BYTES,
    SUPPORTED_REPORT_PROFILES, _native_pdf_text, _validate_image,
    _validate_template, _validate_daily_table_template, _validate_extracted_forecast_text,
)

MAX_WORKING_PIXELS = 4_000_000
MAX_SOURCE_IMAGE_PIXELS = 12_000_000
MAX_WORKING_EDGE = 2800
OCR_TIMEOUT_SECONDS = 10


def _candidate(text,context):
    try:
        if context['report_profile']=='canonical_v1':
            _validate_template(text,context['hotel_code'],context['hotel_name'])
        else:
            text=_validate_daily_table_template(text,context['hotel_code'],context['hotel_name'])
        rows,notices=parse_forecast_text(text)
        errors=validate_forecast(rows)
        if errors:
            return [],['The extracted date/count cells are incomplete or inconsistent.']
        rows=normalize_forecast_rows(rows)
        try:
            _validate_extracted_forecast_text(text,**context)
            return rows,[]
        except ForecastNeedsCorrection as exc:
            # Keep a complete transcription for human review while preserving
            # capacity/arithmetic rejection. Never silently redate.
            return rows,[str(exc)]
    except ForecastNeedsCorrection as exc:
        return [],[str(exc)]


def _review_candidate(text, context):
    rows, issues = _candidate(text, context)
    if rows or context['report_profile'] != 'hotel_daily_table_v1':
        return rows, issues, None
    from services.forecast_layout_review import normalize_known_review_layout
    try:
        normalized, adapter = normalize_known_review_layout(text, context['hotel_code'], context['hotel_name'], context['report_profile'])
    except ForecastNeedsCorrection:
        return rows, issues, None
    rows, issues = _candidate(normalized, dict(context, report_profile='canonical_v1'))
    return rows, issues, adapter


def _ocr_image(data,mime_type):
    from PIL import Image,ImageOps
    if mime_type!='application/pdf':
        _validate_image(data,mime_type,validate_only=True)
        try:
            with Image.open(BytesIO(data)) as source:
                if source.width*source.height>MAX_SOURCE_IMAGE_PIXELS:
                    raise ForecastNeedsCorrection('For bounded local OCR review, resend a photo at12megapixels or less, or the original native PDF.')
                image=ImageOps.exif_transpose(source).convert('RGB')
        except ForecastNeedsCorrection:
            raise
        except Exception as exc:
            raise ForecastNeedsCorrection('The photo could not be decoded for local review. Resend a clear upright report or its native PDF.') from exc
    else:
        # Only reached after the ordinary PDF action/encryption checks reported
        # a scanned page. Multiple/mixed pages are not combined or guessed.
        import pymupdf
        try:
            with pymupdf.open(stream=data,filetype='pdf') as document:
                if len(document)!=1:
                    raise ForecastNeedsCorrection('Scanned PDF review currently requires one complete report page; do not combine different reports.')
                page=document[0]
                if not all(math.isfinite(v) and v>0 for v in (page.rect.width,page.rect.height)):
                    raise ValueError
                scale=min(2.0,MAX_WORKING_EDGE/max(page.rect.width,page.rect.height),
                          math.sqrt(MAX_WORKING_PIXELS/(page.rect.width*page.rect.height)))
                if page.rect.width*page.rect.height*scale*scale>MAX_WORKING_PIXELS:
                    raise ValueError
                pixels=page.get_pixmap(matrix=pymupdf.Matrix(scale,scale),alpha=False)
                image=Image.frombytes('RGB',(pixels.width,pixels.height),pixels.samples)
                # frombytes owns an independent copy; the native raster is no
                # longer needed. Do not retain both through grayscale/OCR.
                del pixels
        except ForecastNeedsCorrection:
            raise
        except Exception as exc:
            raise ForecastNeedsCorrection('The scanned PDF could not be rendered safely for local review.') from exc
    scale=min(1.5,MAX_WORKING_EDGE/max(image.size),math.sqrt(MAX_WORKING_PIXELS/(image.width*image.height)))
    size=tuple(max(1,round(value*scale)) for value in image.size)
    if size!=image.size:
        resized=image.resize(size,Image.Resampling.LANCZOS)
        image.close()
        image=resized
    try:
        gray=ImageOps.grayscale(image)
    finally:
        image.close()
    try:
        return ImageOps.autocontrast(gray)
    finally:
        gray.close()


def _ocr_passes(image):
    import pytesseract
    from services.forecast_import import _configure_tesseract
    _configure_tesseract(pytesseract)
    variants=(('grayscale',image),('thresholded',image.point(lambda value:255 if value>=170 else 0)))
    evidence=[]
    for name,variant in variants:
        try:
            data=pytesseract.image_to_data(variant,config='--psm 6',output_type=pytesseract.Output.DICT,timeout=OCR_TIMEOUT_SECONDS)
            lines={}; numeric=[]
            for index,word in enumerate(data['text']):
                word=str(word).strip()
                if not word:continue
                key=tuple(data[field][index] for field in ('page_num','block_num','par_num','line_num'))
                lines.setdefault(key,[]).append(word)
                if any(character.isdigit() for character in word):
                    confidence=float(data['conf'][index])
                    numeric.append(confidence if math.isfinite(confidence) else -1)
            text='\n'.join(' '.join(words) for words in lines.values())
            if len(text)>100_000:raise ValueError
            evidence.append({'pass':name,'text':text,'numeric_tokens':len(numeric),
                             'low_confidence_numeric_tokens':sum(value<90 for value in numeric),
                             'minimum_numeric_confidence':round(min(numeric),2) if numeric else None})
        except Exception:
            # No raw OCR text, executable paths or source/provider exceptions
            # become an error response. Timeout/missing engine stays fail closed.
            evidence.append({'pass':name,'text':'','numeric_tokens':0,
                             'low_confidence_numeric_tokens':0,'minimum_numeric_confidence':None,
                             'issue':'Local OCR could not complete this bounded pass; manual review or the native PDF is required.'})
    return evidence


def evaluate_forecast_media(data,mime_type,hotel_code,room_capacity,now,*,report_profile='canonical_v1',hotel_name=None):
    """Return complete candidates and admission evidence without saving a roster.

    Capacity/profile/identity must come from trusted saved Parameters/enrollment.
    Historical/future report dates are preserved; the real clock still governs
    workflow approvals and confirmation expiry. OCR is ALWAYS review-only.
    """
    if (not isinstance(report_profile,str) or report_profile not in SUPPORTED_REPORT_PROFILES or not isinstance(hotel_code,str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',hotel_code)
            or type(room_capacity) is not int or not 1<=room_capacity<=100_000
            or not isinstance(now,datetime)):
        raise ValueError('Explicit supported enrollment, saved capacity and reference clock are required.')
    if not isinstance(data,bytes) or not data or mime_type not in {'application/pdf','image/png','image/jpeg'}:
        raise ForecastNeedsCorrection('Supply a nonempty PDF, PNG or JPEG report for local review.')
    if len(data)>(MAX_PDF_BYTES if mime_type=='application/pdf' else MAX_IMAGE_BYTES):
        raise ForecastNeedsCorrection('The report exceeds the bounded PDF/photo size limit.')
    context={'hotel_code':hotel_code,'room_capacity':room_capacity,'now':now,
             'report_profile':report_profile,'hotel_name':hotel_name}
    result={'source_sha256':hashlib.sha256(data).hexdigest(),'reference_time':now.isoformat(),
            'report_profile':report_profile,'room_capacity':room_capacity,'rows':[],
            'automatic_roster_allowed':False,'requires_review':True,'issues':[]}
    scanned=False
    if mime_type=='application/pdf':
        if not data.startswith(b'%PDF-'):raise ForecastNeedsCorrection('The attachment does not match its PDF type.')
        try:text=_native_pdf_text(data)
        except ForecastOCRRequired:scanned=True
        else:
            rows,issues,adapter=_review_candidate(text,context)
            if adapter:
                result.update(method='native_layout_review',rows=rows,issues=issues,
                              status='needs_review' if rows and not issues else 'needs_correction',
                              candidate_valid_for_confirmation=bool(rows and not issues),layout_adapter=adapter)
                return result
            result.update(method='native_pdf',rows=rows,issues=issues,
                          status='eligible' if rows and not issues else 'needs_correction',
                          automatic_roster_allowed=bool(rows and not issues),requires_review=not bool(rows and not issues))
            return result
    image=_ocr_image(data,mime_type)
    try:
        passes=_ocr_passes(image)
    finally:
        image.close()
    candidates=[]; all_issues=[]
    for evidence in passes:
        text=evidence.pop('text')
        rows,issues,adapter=_review_candidate(text,context) if text else ([],[evidence.get('issue','Local OCR did not return usable text.')],None)
        if adapter:evidence['layout_adapter']=adapter
        evidence['complete_cells']=sum(row.get(field) is not None for row in rows for field in NUMERIC_FIELDS)
        evidence['complete_dates']=len(rows)
        evidence['validation_issues']=issues
        candidates.append(rows); all_issues.extend(issues)
    complete=[rows for rows in candidates if len(rows)==7]
    selected_index=next((i for i,rows in enumerate(candidates) if len(rows)==7),None)
    agreement=len(complete)==len(passes) and all(rows==complete[0] for rows in complete)
    if not agreement:all_issues.append('OCR passes are incomplete or disagree. Verify every source date and count; no values were guessed or merged.')
    if any(evidence['low_confidence_numeric_tokens'] for evidence in passes):
        all_issues.append('Some OCR numeric/date tokens have low confidence and need source-cell review.')
    all_issues.append('OCR remains human-review-required even when passes agree; arithmetic and same-engine agreement cannot prove every digit is correct.')
    result.update(method='local_scanned_pdf_ocr' if scanned else 'local_image_ocr',
                  status='needs_review' if complete else 'needs_correction',rows=complete[0] if complete else [],
                  candidate_valid_for_confirmation=selected_index is not None and not passes[selected_index]['validation_issues'],
                  candidate_agreement=agreement,evidence=passes,issues=list(dict.fromkeys(all_issues)))
    return result
