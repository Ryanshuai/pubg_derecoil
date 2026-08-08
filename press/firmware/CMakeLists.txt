# == DO NOT EDIT THE FOLLOWING LINES for the Raspberry Pi Pico VS Code Extension to work ==
if(WIN32)
    set(USERHOME $ENV{USERPROFILE})
else()
    set(USERHOME $ENV{HOME})
endif()
set(sdkVersion 2.2.0)
set(toolchainVersion 14_2_Rel1)
set(picotoolVersion 2.2.0-a4)
set(picoVscode ${USERHOME}/.pico-sdk/cmake/pico-vscode.cmake)
if (EXISTS ${picoVscode})
    include(${picoVscode})
endif()
# ====================================================================================
set(PICO_BOARD pico2 CACHE STRING "Board type")

cmake_minimum_required(VERSION 3.13)

if(NOT DEFINED PICO_PLATFORM)
    set(PICO_PLATFORM rp2350-arm-s)
endif()

include(pico_sdk_import.cmake)

project(pico_mouse C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

pico_sdk_init()

# ── PIO USB library ──────────────────────────────────────
set(PIO_USB_DIR ${CMAKE_CURRENT_LIST_DIR}/pio_usb)

add_library(pico_pio_usb INTERFACE)

pico_generate_pio_header(pico_pio_usb ${PIO_USB_DIR}/usb_tx.pio)
pico_generate_pio_header(pico_pio_usb ${PIO_USB_DIR}/usb_rx.pio)

target_sources(pico_pio_usb INTERFACE
    ${PIO_USB_DIR}/pio_usb.c
    ${PIO_USB_DIR}/pio_usb_device.c
    ${PIO_USB_DIR}/pio_usb_host.c
    ${PIO_USB_DIR}/usb_crc.c
    ${PIO_USB_DIR}/interval_override.c
)

target_link_libraries(pico_pio_usb INTERFACE
    pico_stdlib
    pico_multicore
    hardware_pio
    hardware_dma
)

target_include_directories(pico_pio_usb INTERFACE ${PIO_USB_DIR})

# ── Main executable ──────────────────────────────────────
set(PICO_TINYUSB_PATH ${PICO_SDK_PATH}/lib/tinyusb)

add_executable(pico_mouse
    src/main.c
    src/usb_descriptors.c
    ${PICO_TINYUSB_PATH}/src/portable/raspberrypi/pio_usb/dcd_pio_usb.c
    ${PICO_TINYUSB_PATH}/src/portable/raspberrypi/pio_usb/hcd_pio_usb.c
)

# ../protocol is press/protocol, carrying protocol.h — GENERATED from the
# protocol.toml beside it by tools/gen_protocol.py, and committed. It is on the include path rather than
# copied in so that there is exactly one file: a copy would compile fine while
# disagreeing with what press/pico_mouse.py sends, which is the whole failure
# this indirection exists to make impossible.
#
# The generator is deliberately NOT wired in as a build step — the Pico build
# must not require Python. `pixi run gen-protocol --check` is what catches a
# stale header, and it runs from flash.py before it writes anything.
target_include_directories(pico_mouse PRIVATE src ${CMAKE_CURRENT_LIST_DIR}/../protocol)

target_compile_definitions(pico_mouse PRIVATE PIO_USB_USE_TINYUSB)

target_link_libraries(pico_mouse
    pico_stdlib
    pico_pio_usb
    tinyusb_device
    tinyusb_host
)

pico_enable_stdio_usb(pico_mouse 0)
pico_enable_stdio_uart(pico_mouse 0)

pico_add_extra_outputs(pico_mouse)
