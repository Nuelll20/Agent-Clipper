import cv2

print("Versi:", cv2.__version__)
print("Lokasi:", cv2.__file__)
print(
    "CascadeClassifier:",
    hasattr(cv2, "CascadeClassifier")
)

if hasattr(cv2, "data"):
    print("Folder Haar:", cv2.data.haarcascades)
else:
    print("Folder Haar: cv2.data tidak tersedia")
