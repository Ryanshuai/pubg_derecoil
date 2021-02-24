# Pubg derecoil

1 install all python libraries including pytorch, cv2, pyinput\
2 change all the positions in state.position_constants.py\
this step means telling the program which area to be cropped for image detection\
![](readme/495.png)

| position need to be fine tune  |
| ----        |
| 'in_tab' under the inventory for checking if it is in tab | 
| 'gun1_name' | 
| 'gun1_scope' | 
| 'gun1_muzzle' | 
| 'gun1_grip' | 
| 'gun1_butt' | 
| 'in_tab' | 
| 'gun2_name' | 
| 'gun2_scope' | 
| 'gun2_muzzle' | 
| 'gun2_grip' | 
| 'gun2_butt' | 

the ratio of height to width should be similar to original\
&nbsp; &nbsp; in the list named crop_position:\
&nbsp; &nbsp; &nbsp; &nbsp; when there are four values:\
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; it means [x1, y1, x2, y2]\
&nbsp; &nbsp; &nbsp; &nbsp; when there are only two values:\
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; it means [x1, y1], and the x2=x1+64, y2=x1+64\
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; gun scope muzzle grip butt 's width, height is 64, 64 \
&nbsp; &nbsp; &nbsp; &nbsp;change "type" positions in state.position_constants.py\
&nbsp; &nbsp; &nbsp; &nbsp; if your screen shows this:\
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp;-full-1---\
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp;-full-1---\
&nbsp; &nbsp; &nbsp; &nbsp; most likely because you did not set "type" position correctly\
3 try to debug in pubg training mode \
&nbsp; &nbsp; after you grab guns and attachments, then push tab button on your keyboard.The name of the weapon, all
types of attachments should be shown in the white area on your screen. \
&nbsp; &nbsp; The cropped image will be saved in for_data_check dir, where you can check the images there to determine whether
your position settings are correct. 