# Document fixtures

`english-cv.doc` contains the fictional candidate from the frontend's `sample-cv.docx`. LibreOffice in the pinned Gotenberg 8.39.0 image generated its Word 97 binary format. It contains no real candidate information or macros.

Automated tests validate this DOC and use the local converter/OCR interfaces. The separate conversion smoke checks the pinned service against this fixture.
