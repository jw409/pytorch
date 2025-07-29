mkdir %TMP_DIR_WIN%\bin

if "%REBUILD%"=="" (
  IF EXIST %TMP_DIR_WIN%\bin\sccache.exe (
    taskkill /im sccache.exe /f /t || ver > nul
    del %TMP_DIR_WIN%\bin\sccache.exe || ver > nul
  )
  if "%BUILD_ENVIRONMENT%"=="" (
    curl --retry 3 --retry-all-errors -k https://s3.amazonaws.com/ossci-windows/sccache-8c07bf16821550692101b8db5855d930585bb3e9-x86_64-pc-windows-msvc.exe --output %TMP_DIR_WIN%\bin\sccache.exe
  ) else (
    aws s3 cp s3://ossci-windows/sccache-8c07bf16821550692101b8db5855d930585bb3e9-x86_64-pc-windows-msvc.exe %TMP_DIR_WIN%\bin\sccache.exe
  )
)
