load("@rules_cc//cc:defs.bzl", "cc_library")
load("@rules_cuda//cuda:defs.bzl", "cuda_library")

package(default_visibility = ["//visibility:public"])

licenses(["notice"])

exports_files(["COPYING"])

COPTS = [
    "-Wno-unused-function",
    "-Wno-implicit-function-declaration",
    "-Wno-int-conversion",
    "-D_GNU_SOURCE",
]

LINKOPTS = ["-lpthread"]

cc_library(
    name = "ggml-base",
    srcs = glob([
        "ggml/src/*.c",
        "ggml/src/*.cpp",
    ]),
    hdrs = glob([
        "ggml/include/*.h",
        "ggml/src/*.h",
        "ggml/src/ggml-cpu/*.h",
    ]),
    includes = [
        "ggml/include",
        "ggml/src",
    ],
    copts = COPTS,
    local_defines = [
        "GGML_VERSION='\"0.0.0\"'",
        "GGML_COMMIT='\"ztab\"'",
        "GGML_USE_CPU",
    ] + select({
        "@//tee:cuda_enabled": ["GGML_USE_CUDA"],
        "//conditions:default": [],
    }),
    linkopts = LINKOPTS,
)

cc_library(
    name = "ggml-cpu",
    srcs = glob([
        "ggml/src/ggml-cpu/*.c",
        "ggml/src/ggml-cpu/*.cpp",
        "ggml/src/ggml-cpu/amx/*.cpp",
        "ggml/src/ggml-cpu/arch/x86/*.cpp",
        "ggml/src/ggml-cpu/arch/x86/*.c",
    ]),
    hdrs = glob([
        "ggml/src/ggml-cpu/*h",
        "ggml/src/ggml-cpu/amx/*h",
    ]),
    includes = [
        "ggml/include",
        "ggml/src",
        "ggml/src/ggml-cpu",
    ],
    copts = COPTS,
    deps = [
        ":ggml-base",
    ],
    alwayslink = True,
    linkopts = LINKOPTS,
)

cuda_library(
    name = "ggml-cuda",
    srcs = glob([
        "ggml/src/ggml-cuda/**/*.cu",
    ]),
    hdrs = glob([
        "ggml/src/ggml-cuda/**/*.cuh",
        "ggml/src/ggml-cuda/**/*.h",
    ]),
    includes = [
        "ggml/src",
    ],
    host_copts = [
        "-stdlib=libstdc++",
        "-Wno-pedantic",
    ],
    copts = [
        "-allow-unsupported-compiler",
        "-use_fast_math",
        "-extended-lambda",
    ],
    local_defines = [
        "GGML_CUDA_FORCE_CUBLAS",
    ],
    deps = [
        ":ggml-base",
        "@cuda_curand//:curand",
        "@cuda_cublas//:cublas",
        "@nvidia_driver//:cuda_libs",
    ],
    alwayslink = True,
)

genrule(
    name = "build_info_gen",
    outs = ["build-info.cpp"],
    cmd = """cat << 'EOF' > $@
int llama_build_number(void) { return 0; }
const char * llama_commit(void) { return "ztab"; }
const char * llama_compiler(void) { return "clang"; }
const char * llama_build_target(void) { return "x86_64"; }
EOF
""",
)

cc_library(
    name = "llama_cpp",
    srcs = [":build_info_gen"] + glob([
        "src/**/*.cpp",
        "common/*.cpp",
    ], exclude = [
        # Needs nlohmann/json
        "common/json-schema-to-grammar.cpp",
        "common/json-partial.cpp",
        "common/arg.cpp",
        "common/preset.cpp",
        # Needs nlohmann/json + jinja templates
        "common/chat.cpp",
        "common/chat-auto-parser-generator.cpp",
        "common/chat-auto-parser-helpers.cpp",
        "common/chat-diff-analyzer.cpp",
        "common/chat-peg-parser.cpp",
        "common/peg-parser.cpp",
        # Needs libcurl
        "common/download.cpp",
        "common/hf-cache.cpp",
        # Needs llguidance external library
        "common/llguidance.cpp",
        # Terminal I/O not needed in server
        "common/console.cpp",
    ]),
    hdrs = glob([
        "include/*.h",
        "src/**/*.h",
        "common/*.h",
        "common/*.hpp",
    ]),
    includes = [
        "common",
        "include",
        "src",
    ],
    copts = COPTS,
    deps = [
        ":ggml-base",
        ":ggml-cpu",
    ] + select({
        "@//tee:cuda_enabled": [":ggml-cuda"],
        "//conditions:default": [],
    }),
)
