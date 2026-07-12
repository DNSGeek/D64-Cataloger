; D64Catalog.pb - PureBasic GUI front end for the d64catalog SQLite database.
;
; Layout follows the mockup: library root + SCAN, search box, format
; include checkboxes, find-mode checkboxes, results list.
;
; Scanning shells out to d64catalog.py (non-blocking, output streamed to
; the status line). Searching queries the SQLite database directly using
; FTS5 when available, with a LIKE fallback otherwise.
;
; Tested against PureBasic 6.x syntax. Adjust #PYTHON$ if your python3
; lives somewhere exotic.

EnableExplicit

;- Configuration --------------------------------------------------------------

#PYTHON$ = "python3"                       ; interpreter used for scans
Global gScript$ = GetPathPart(ProgramFilename()) + "d64catalog.py"
Global gDbPath$ = GetHomeDirectory() + "d64catalog.db"

;- Identifiers ----------------------------------------------------------------

Enumeration Windows
  #WinMain
EndEnumeration

Enumeration Gadgets
  #TxtRoot
  #StrRoot
  #BtnRootBrowse
  #BtnScan
  #TxtDb
  #StrDb
  #BtnDbBrowse
  #BtnDbNew
  #TxtSearch
  #StrSearch
  #BtnSearch
  #TxtInclude
  #ChkD64
  #ChkD71
  #ChkD81
  #ChkTAP
  #ChkT64
  #ChkPRG
  #ChkCRT
  #TxtFind
  #ChkImageName
  #ChkFileName
  #LstResults
  #TxtStatus
EndEnumeration

Enumeration Shortcuts
  #ShortcutSearch
EndEnumeration

#DB = 0

Global gScanProg.i = 0        ; RunProgram handle while a scan is active
Global gFtsAvailable.i = -1   ; -1 unknown, 0 no, 1 yes

;- Database helpers -----------------------------------------------------------

Procedure.s NormalizePath(p$)
  ; PB's file functions do not expand ~ the way a shell does; a typed
  ; path like ~/catalog.db silently fails FileSize(). Expand it here.
  p$ = Trim(p$)
  If p$ = "~"
    p$ = GetHomeDirectory()
  ElseIf Left(p$, 2) = "~/" Or Left(p$, 2) = "~\"
    p$ = GetHomeDirectory() + Mid(p$, 3)
  EndIf
  ProcedureReturn p$
EndProcedure

Procedure SyncDbPath()
  ; The StrDb gadget is the source of truth. If the user pointed at a
  ; different database, forget what we knew about the old one's FTS index.
  Protected new$ = NormalizePath(GetGadgetText(#StrDb))
  If new$ <> "" And new$ <> gDbPath$
    gDbPath$ = new$
    gFtsAvailable = -1
  EndIf
EndProcedure

Procedure.i DbOpen()
  SyncDbPath()
  ; FileSize: -1 = not found, -2 = directory. A 0-byte file is a valid
  ; (empty) SQLite database, so only reject genuinely missing paths.
  If FileSize(gDbPath$) = -1
    SetGadgetText(#TxtStatus, "Database not found: " + gDbPath$ +
                              "  (scan to create it)")
    ProcedureReturn #False
  EndIf
  If FileSize(gDbPath$) = -2
    SetGadgetText(#TxtStatus, "That is a directory, not a database: " +
                              gDbPath$)
    ProcedureReturn #False
  EndIf
  If OpenDatabase(#DB, gDbPath$, "", "")
    ProcedureReturn #True
  EndIf
  SetGadgetText(#TxtStatus, "Cannot open database: " + gDbPath$)
  ProcedureReturn #False
EndProcedure

Procedure.i ProbeFts()
  ; Cache the answer; probe once per database open session.
  If gFtsAvailable = -1
    If DatabaseQuery(#DB, "SELECT fts5('')")
      FinishDatabaseQuery(#DB)
      ; FTS5 compiled in - but does this DB have the index?
      If DatabaseQuery(#DB, "SELECT name FROM sqlite_master " +
                            "WHERE type='table' AND name='search_fts'")
        If NextDatabaseRow(#DB)
          gFtsAvailable = 1
        Else
          gFtsAvailable = 0
        EndIf
        FinishDatabaseQuery(#DB)
      Else
        gFtsAvailable = 0
      EndIf
    Else
      gFtsAvailable = 0
    EndIf
  EndIf
  ProcedureReturn gFtsAvailable
EndProcedure

Procedure.s FtsEscape(raw$)
  ; Turn user input into a safe FTS5 query: each whitespace-separated
  ; term becomes a quoted prefix token: boulder dash -> "boulder"* "dash"*
  Protected result$ = "", term$, i.i, count.i
  Protected clean$ = Trim(raw$)
  count = CountString(clean$, " ") + 1
  For i = 1 To count
    term$ = Trim(StringField(clean$, i, " "))
    If term$ <> ""
      term$ = ReplaceString(term$, Chr(34), Chr(34) + Chr(34))
      result$ + Chr(34) + term$ + Chr(34) + "* "
    EndIf
  Next
  ProcedureReturn Trim(result$)
EndProcedure

Procedure.s TypeFilterSql()
  ; Build "AND d.image_type IN ('D64','D81',...)" from the checkboxes.
  ; No boxes checked = no filter (show everything).
  Protected list$ = ""
  If GetGadgetState(#ChkD64) : list$ + "'D64'," : EndIf
  If GetGadgetState(#ChkD71) : list$ + "'D71'," : EndIf
  If GetGadgetState(#ChkD81) : list$ + "'D81'," : EndIf
  If GetGadgetState(#ChkTAP) : list$ + "'TAP'," : EndIf
  If GetGadgetState(#ChkT64) : list$ + "'T64'," : EndIf
  If GetGadgetState(#ChkPRG) : list$ + "'PRG'," : EndIf
  If GetGadgetState(#ChkCRT) : list$ + "'CRT'," : EndIf
  If list$ = ""
    ProcedureReturn ""
  EndIf
  ProcedureReturn " AND d.image_type IN (" + RTrim(list$, ",") + ")"
EndProcedure

Procedure AddResultRow(name$, disk$, type$, path$)
  AddGadgetItem(#LstResults, -1, name$ + Chr(10) + disk$ + Chr(10) +
                                 type$ + Chr(10) + path$)
EndProcedure

Procedure.i QueryImageNames(raw$)
  ; One row per matching IMAGE (filename or diskname match).
  Protected rows.i = 0, ok.i
  If ProbeFts()
    Protected match$ = "{filename diskname} : (" + FtsEscape(raw$) + ")"
    SetDatabaseString(#DB, 0, match$)
    ok = DatabaseQuery(#DB,
        "SELECT DISTINCT d.filename, d.diskname, d.image_type, d.path " +
        "FROM search_fts s JOIN disks d ON d.id = s.disk_id " +
        "WHERE search_fts MATCH ?" + TypeFilterSql() +
        " ORDER BY d.filename LIMIT 500")
  Else
    SetDatabaseString(#DB, 0, "%" + raw$ + "%")
    SetDatabaseString(#DB, 1, "%" + raw$ + "%")
    ok = DatabaseQuery(#DB,
        "SELECT d.filename, d.diskname, d.image_type, d.path FROM disks d " +
        "WHERE (d.filename LIKE ? OR d.diskname LIKE ?)" + TypeFilterSql() +
        " ORDER BY d.filename LIMIT 500")
  EndIf
  If ok
    While NextDatabaseRow(#DB)
      AddResultRow(GetDatabaseString(#DB, 0), GetDatabaseString(#DB, 1),
                   GetDatabaseString(#DB, 2), GetDatabaseString(#DB, 3))
      rows + 1
    Wend
    FinishDatabaseQuery(#DB)
  Else
    SetGadgetText(#TxtStatus, "Query error: " + DatabaseError())
    ProcedureReturn -1
  EndIf
  ProcedureReturn rows
EndProcedure

Procedure.i QueryFileNames(raw$)
  ; One row per matching FILE inside an image.
  Protected rows.i = 0, ok.i, blocks$
  If ProbeFts()
    Protected match$ = "{name} : (" + FtsEscape(raw$) + ")"
    SetDatabaseString(#DB, 0, match$)
    ok = DatabaseQuery(#DB,
        "SELECT f.name, d.diskname, f.file_type, d.path " +
        "FROM search_fts s " +
        "JOIN files f ON f.id = s.file_id " +
        "JOIN disks d ON d.id = s.disk_id " +
        "WHERE search_fts MATCH ?" + TypeFilterSql() +
        " ORDER BY rank LIMIT 500")
  Else
    SetDatabaseString(#DB, 0, "%" + raw$ + "%")
    ok = DatabaseQuery(#DB,
        "SELECT f.name, d.diskname, f.file_type, d.path " +
        "FROM files f JOIN disks d ON d.id = f.disk_id " +
        "WHERE f.name LIKE ?" + TypeFilterSql() +
        " ORDER BY f.name LIMIT 500")
  EndIf
  If ok
    While NextDatabaseRow(#DB)
      AddResultRow(GetDatabaseString(#DB, 0), GetDatabaseString(#DB, 1),
                   GetDatabaseString(#DB, 2), GetDatabaseString(#DB, 3))
      rows + 1
    Wend
    FinishDatabaseQuery(#DB)
  Else
    SetGadgetText(#TxtStatus, "Query error: " + DatabaseError())
    ProcedureReturn -1
  EndIf
  ProcedureReturn rows
EndProcedure

Procedure RunSearch()
  Protected raw$ = Trim(GetGadgetText(#StrSearch))
  Protected total.i = 0, r.i

  ClearGadgetItems(#LstResults)
  If raw$ = ""
    SetGadgetText(#TxtStatus, "Enter a search term.")
    ProcedureReturn
  EndIf
  If gScanProg
    SetGadgetText(#TxtStatus, "Scan in progress - wait for it to finish.")
    ProcedureReturn
  EndIf
  If DbOpen() = #False
    ProcedureReturn
  EndIf

  ; Neither find-mode checked: treat as both (a search that can never
  ; match anything is not a useful interpretation of the UI state).
  Protected wantImage.i = GetGadgetState(#ChkImageName)
  Protected wantFile.i = GetGadgetState(#ChkFileName)
  If wantImage = #False And wantFile = #False
    wantImage = #True
    wantFile = #True
  EndIf

  If wantImage
    r = QueryImageNames(raw$)
    If r >= 0 : total + r : EndIf
  EndIf
  If wantFile
    r = QueryFileNames(raw$)
    If r >= 0 : total + r : EndIf
  EndIf

  CloseDatabase(#DB)
  gFtsAvailable = -1   ; re-probe next time; a scan may rebuild the index
  SetGadgetText(#TxtStatus, Str(total) + " result(s) for: " + raw$)
EndProcedure

;- Scanning -------------------------------------------------------------------

Procedure StartScan()
  Protected root$ = NormalizePath(GetGadgetText(#StrRoot))
  SyncDbPath()
  If gScanProg
    SetGadgetText(#TxtStatus, "A scan is already running.")
    ProcedureReturn
  EndIf
  If root$ = "" Or FileSize(root$) <> -2   ; -2 = directory
    SetGadgetText(#TxtStatus, "Library root is not a directory: " + root$)
    ProcedureReturn
  EndIf
  If FileSize(gScript$) <= 0
    SetGadgetText(#TxtStatus, "Scanner not found: " + gScript$)
    ProcedureReturn
  EndIf

  Protected args$ = Chr(34) + gScript$ + Chr(34) + " scan " +
                    Chr(34) + root$ + Chr(34) + " " +
                    Chr(34) + gDbPath$ + Chr(34)
  gScanProg = RunProgram(#PYTHON$, args$, "",
                         #PB_Program_Open | #PB_Program_Read |
                         #PB_Program_Error | #PB_Program_Hide)
  If gScanProg
    DisableGadget(#BtnScan, #True)
    SetGadgetText(#TxtStatus, "Scanning " + root$ + " ...")
  Else
    SetGadgetText(#TxtStatus, "Failed to launch: " + #PYTHON$)
  EndIf
EndProcedure

Procedure PollScan()
  Protected line$
  If gScanProg = 0
    ProcedureReturn
  EndIf
  While AvailableProgramOutput(gScanProg)
    line$ = ReadProgramString(gScanProg)
    If line$ <> ""
      SetGadgetText(#TxtStatus, line$)
    EndIf
  Wend
  If ProgramRunning(gScanProg) = #False
    line$ = ReadProgramError(gScanProg)   ; surface last stderr line, if any
    Protected code.i = ProgramExitCode(gScanProg)
    CloseProgram(gScanProg)
    gScanProg = 0
    gFtsAvailable = -1
    DisableGadget(#BtnScan, #False)
    If code = 0
      SetGadgetText(#TxtStatus, GetGadgetText(#TxtStatus) + "  Scan complete.")
    Else
      SetGadgetText(#TxtStatus, "Scan failed (exit " + Str(code) + "): " +
                                line$)
    EndIf
  EndIf
EndProcedure

;- Layout ---------------------------------------------------------------------

Procedure BrowseRoot()
  Protected p$ = PathRequester("Select library root",
                               GetGadgetText(#StrRoot))
  If p$ <> ""
    SetGadgetText(#StrRoot, p$)
  EndIf
EndProcedure

Procedure BrowseDbOpen()
  ; Picking an EXISTING catalog: OpenFileRequester, no overwrite scares.
  Protected init$ = NormalizePath(GetGadgetText(#StrDb))
  Protected p$ = OpenFileRequester("Open catalog database", init$,
                                   "SQLite database (*.db)|*.db|" +
                                   "All files (*.*)|*.*", 0)
  If p$ <> ""
    SetGadgetText(#StrDb, p$)
    SyncDbPath()
    SetGadgetText(#TxtStatus, "Database: " + gDbPath$)
  EndIf
EndProcedure

Procedure BrowseDbNew()
  ; Creating a NEW catalog: SaveFileRequester lets the user type a name.
  Protected init$ = NormalizePath(GetGadgetText(#StrDb))
  Protected p$ = SaveFileRequester("Create catalog database", init$,
                                   "SQLite database (*.db)|*.db|" +
                                   "All files (*.*)|*.*", 0)
  If p$ <> ""
    If GetExtensionPart(p$) = ""
      p$ + ".db"
    EndIf
    SetGadgetText(#StrDb, p$)
    SyncDbPath()
    SetGadgetText(#TxtStatus, "New database: " + gDbPath$ +
                              "  (scan to populate it)")
  EndIf
EndProcedure

Procedure ResizeUi()
  Protected w.i = WindowWidth(#WinMain)
  Protected h.i = WindowHeight(#WinMain)
  ResizeGadget(#StrRoot, 120, 12, w - 310, 24)
  ResizeGadget(#BtnRootBrowse, w - 180, 12, 70, 24)
  ResizeGadget(#BtnScan, w - 100, 12, 90, 24)
  ResizeGadget(#StrDb, 120, 46, w - 310, 24)
  ResizeGadget(#BtnDbBrowse, w - 180, 46, 70, 24)
  ResizeGadget(#BtnDbNew, w - 100, 46, 90, 24)
  ResizeGadget(#LstResults, 10, 148, w - 20, h - 188)
  ResizeGadget(#TxtStatus, 10, h - 30, w - 20, 20)
EndProcedure

Procedure BuildUi()
  OpenWindow(#WinMain, 0, 0, 970, 690, "D64Catalog",
             #PB_Window_SystemMenu | #PB_Window_MinimizeGadget |
             #PB_Window_MaximizeGadget | #PB_Window_SizeGadget |
             #PB_Window_ScreenCentered)
  WindowBounds(#WinMain, 970, 450, #PB_Ignore, #PB_Ignore)

  TextGadget(#TxtRoot, 10, 16, 105, 20, "Library root:",
             #PB_Text_Right)
  StringGadget(#StrRoot, 120, 12, 660, 24, GetHomeDirectory())
  ButtonGadget(#BtnRootBrowse, 790, 12, 70, 24, "Browse...")
  ButtonGadget(#BtnScan, 870, 12, 90, 24, "SCAN")

  TextGadget(#TxtDb, 10, 50, 105, 20, "Database:", #PB_Text_Right)
  StringGadget(#StrDb, 120, 46, 660, 24, gDbPath$)
  ButtonGadget(#BtnDbBrowse, 790, 46, 70, 24, "Open...")
  ButtonGadget(#BtnDbNew, 870, 46, 90, 24, "New...")

  TextGadget(#TxtSearch, 10, 84, 105, 20, "Search :", #PB_Text_Right)
  StringGadget(#StrSearch, 120, 80, 330, 24, "")
  ButtonGadget(#BtnSearch, 455, 80, 40, 24, "Go")

  TextGadget(#TxtInclude, 520, 84, 60, 20, "Include:")
  CheckBoxGadget(#ChkD64, 582, 82, 52, 20, "D64")
  CheckBoxGadget(#ChkD71, 636, 82, 52, 20, "D71")
  CheckBoxGadget(#ChkD81, 690, 82, 52, 20, "D81")
  CheckBoxGadget(#ChkTAP, 744, 82, 52, 20, "TAP")
  CheckBoxGadget(#ChkT64, 798, 82, 52, 20, "T64")
  CheckBoxGadget(#ChkPRG, 852, 82, 52, 20, "PRG")
  CheckBoxGadget(#ChkCRT, 906, 82, 54, 20, "CRT")

  ; Defaults follow the mockup: D64/D71/D81/CRT on, TAP/T64/PRG off.
  SetGadgetState(#ChkD64, #PB_Checkbox_Checked)
  SetGadgetState(#ChkD71, #PB_Checkbox_Checked)
  SetGadgetState(#ChkD81, #PB_Checkbox_Checked)
  SetGadgetState(#ChkCRT, #PB_Checkbox_Checked)

  TextGadget(#TxtFind, 10, 118, 105, 20, "Find :", #PB_Text_Right)
  CheckBoxGadget(#ChkImageName, 120, 116, 120, 20, "Image name")
  CheckBoxGadget(#ChkFileName, 250, 116, 200, 20, "File name inside image")
  SetGadgetState(#ChkImageName, #PB_Checkbox_Checked)

  ListIconGadget(#LstResults, 10, 148, 950, 502, "Name", 380,
                 #PB_ListIcon_FullRowSelect | #PB_ListIcon_AlwaysShowSelection)
  AddGadgetColumn(#LstResults, 1, "Disk", 150)
  AddGadgetColumn(#LstResults, 2, "Type", 55)
  AddGadgetColumn(#LstResults, 3, "Path", 340)

  TextGadget(#TxtStatus, 10, 660, 950, 20,
             "Database: " + gDbPath$)

  AddKeyboardShortcut(#WinMain, #PB_Shortcut_Return, #ShortcutSearch)
EndProcedure

;- Main -----------------------------------------------------------------------

UseSQLiteDatabase()
BuildUi()

Define event.i, quit.i = #False

Repeat
  If gScanProg
    event = WaitWindowEvent(100)   ; keep polling while a scan runs
    PollScan()
  Else
    event = WaitWindowEvent()
  EndIf

  Select event
    Case #PB_Event_CloseWindow
      quit = #True

    Case #PB_Event_SizeWindow
      ResizeUi()

    Case #PB_Event_Menu
      If EventMenu() = #ShortcutSearch
        RunSearch()
      EndIf

    Case #PB_Event_Gadget
      Select EventGadget()
        Case #BtnScan
          StartScan()
        Case #BtnSearch
          RunSearch()
        Case #BtnRootBrowse
          BrowseRoot()
        Case #BtnDbBrowse
          BrowseDbOpen()
        Case #BtnDbNew
          BrowseDbNew()
      EndSelect
  EndSelect
Until quit

If gScanProg
  KillProgram(gScanProg)
  CloseProgram(gScanProg)
EndIf
End

; IDE Options = PureBasic 6.41 beta 3 - C Backend (MacOS X - arm64)
; Folding = ---
; EnableThread
; EnableXP
; DPIAware
; Executable = d64catalog.app