Using the "build-res.bat" and "Make_Resource.qsc" you can build all the models into a resource file. It also generates ".mtp" and ".dat" file which is very essential for the game to read model data.
The model files (.mef) should be in ASCII format.


Steps to Create .res out of .mef files :-
1> Keep all your ASCII model files into "input" directory.
2> edit "Make_Resource.qsc" with notepad and replace "CreateCompleteModelRigid("");" lines with your custom script and id
(eg..CreateCompleteModelRigid("model file name");
     CreateCompleteModelRigid("model file name");
     CreateCompleteModelRigid("model file name"); etc)
3> Run "build-res.bat" and wait for the process to finish...
4> you will get all your ".res" and ".dat, .mtp" files inside your "output" folder.


Steps to Extract .res files :-
1> Keep all your ".res" files inside "input" folder.
2> Run "extract-res.bat" and your will get extracted resources in output folder.


About the files :-
.res -> a compiled file contains multiple resources such as .tex, .tga, .spr, .mef etc...
.tex -> a compiled tga image file
.tga -> a new generation image file extension
.spr -> same as .tex file
.mef (ASCII) -> model file used by IGI games to show objects (readable by human)
.mef (Binary) -> model file used by IGI games to show objects (computer readable only)
.max -> a temporary model file used by igi converters to compile in .res