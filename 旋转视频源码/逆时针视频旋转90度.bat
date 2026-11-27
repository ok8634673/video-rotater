md newfiles
for %%a in (*.mp4) do ffmpeg  -display_rotation:v:0 90 -i %%a -c copy newfiles\%%~na.mp4
pause