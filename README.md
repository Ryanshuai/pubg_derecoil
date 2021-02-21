# Pubg derecoil

 1 install all python libraries including pytorch, cv2, pyinput\
 2 change all the positions in state.position_constants.py\
this step means telling the program which area to be cropped for image detection\
![](readme/495.png)
the ratio of height to width should be similar to original\
 &nbsp; &nbsp; in the list named crop_position:\
 &nbsp; &nbsp; &nbsp; &nbsp; when there are four values:\
 &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; it means [x1, y1, x2, y2]\
 &nbsp; &nbsp; &nbsp; &nbsp; when there are only two values:\
 &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; it means [x1, y1], and the x2=x1+64, y2=x1+64\
 &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; gun scope muzzle grip butt 's width, height is 64, 64 \
 3 try to debug in pubg training mode \
  &nbsp; &nbsp; after you grab guns and attachments, then push tab button in your keyboard.The name of the weapon, all types of attachments should be shown in the white area on your screen. 