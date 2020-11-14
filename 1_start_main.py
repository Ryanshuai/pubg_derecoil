from PyQt5 import QtCore, QtGui, QtWidgets

from screen_parameter import show_position_y, show_position_x, show_size_y, show_size_x
from press_gun.robot import Robot
from state.all_states import All_States


class Ui_Dialog(object):
    def setupUi(self, Dialog):
        Dialog.setObjectName("Dialog")
        Dialog.resize(show_size_x, show_size_y)
        Dialog.move(show_position_x, show_position_y)
        Dialog.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint)
        Dialog.setWindowFlag(QtCore.Qt.FramelessWindowHint)
        # Dialog.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        font = QtGui.QFont()
        font.setFamily("Arial")
        font.setPointSize(14)
        Dialog.setFont(font)

        self.label = QtWidgets.QLabel(Dialog)
        self.label.setGeometry(QtCore.QRect(0, 0, show_size_x, show_size_y))
        self.label.setObjectName("label")

        QtCore.QMetaObject.connectSlotsByName(Dialog)

        # self.robot = Robot(All_States(), is_calibrating=True)
        self.robot = Robot(All_States())
        self.robot.temp_qobject.state_str_signal[str].connect(self.retranslateUi)

    def retranslateUi(self, text):
        _translate = QtCore.QCoreApplication.translate
        self.label.setText(_translate("Dialog", text))


if __name__ == "__main__":
    import sys

    app = QtWidgets.QApplication(sys.argv)
    Dialog = QtWidgets.QDialog()
    ui = Ui_Dialog()
    ui.setupUi(Dialog)
    Dialog.show()
    sys.exit(app.exec_())
