# eprtool


## Running
### Download (standalone executables)
- [Windows EXE (64-bit)](https://github.com/xypwn/eprtool/releases/latest/download/eprtool.exe)
- [Linux executable (64-bit, archived)](https://github.com/xypwn/eprtool/releases/latest/download/eprtool-linux.tar.xz)

Note that the executables quite big, since they include [typst](https://github.com/typst/typst) for PDF generation, as well as other large libraries.

### uvx
```sh
uvx --from git+https://github.com/xypwn/eprtool eprtool
```

## Usage
#### 0. Configure settings
In "Settings":
- Set "tutor" to the title of your tutoring group(s) (e.g. "EPR 01/02 - Firstname")
- Set "pdf_annnotated_suffix" to the suffix your PDF annotator adds to annotated filenames (e.g. for xournalpp, `_kommentiert` in German, `_annotated` in English etc.)

#### 1. Open submission ZIP
Use the "Open submission ZIP" button to select a ZIP file downloaded from moodle via "Download all submissions".
#### 2. Select title
In the "title" text box, set the task title, e.g. "Uebung 0" (since it cannot be inferred automatically).
#### 3. Organize files
You will see all submissions in the left. Go through all of them and make sure all files are in the right order (drag to rearrange), have the right name and tag (double-click to edit).

Available tags are:
- `code`: all combined into `code.pdf` in listing order (use this for program code or Markdown documentation)
- `documentation`: each documentation file is copied verbatim (use this for PDF documentation)
- `ignore`: completely ignored

#### 5. Export to feedback folder
Click on "Export to feedback folder" to export everything into a folder where you can make annotations.

The output will be a folder with your selected title as the name inside the selected folder.

#### 6. Annotate files
Recommended software: [xournalpp](https://xournalpp.github.io/).

Make sure your settings are set correctly for your chosen PDF annotator and language (see 0.).

#### 7. Repack ZIP
Once every PDF has been annotated, click "Pack feedback ZIP". Select the feedback folder. A .zip file will appear inside, which can be uploaded to Moodle via "Upload feedback files as ZIP".