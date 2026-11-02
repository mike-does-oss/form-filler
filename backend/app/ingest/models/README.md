# Bundled OCR models

`en_PP-OCRv3_rec_infer.onnx` — English text recognition model from PaddleOCR (PP-OCRv3), converted
to ONNX by the RapidOCR project (https://huggingface.co/SWHL/RapidOCR). Apache License 2.0.

RapidOCR's wheel ships Chinese detection/recognition models; the Chinese recogniser drops the
spaces between Latin words, so for `ocr.language: en` we swap in this model. Detection still
uses the bundled model.
