import ctranslate2

print("CTranslate2:", ctranslate2.__version__)

try:
    gpu_count = ctranslate2.get_cuda_device_count()
    print("Jumlah GPU CUDA:", gpu_count)

    if gpu_count > 0:
        compute_types = ctranslate2.get_supported_compute_types(
            "cuda"
        )
        print("Compute type GPU:", compute_types)
    else:
        print("GPU NVIDIA belum terdeteksi oleh CTranslate2.")
except Exception as error:
    print("Pemeriksaan GPU gagal:")
    print(type(error).__name__ + ":", error)
