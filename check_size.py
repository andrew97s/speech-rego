import os
import site

site_packages = site.getsitepackages()[0]

def get_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.path.getsize(fp)
            except:
                pass
    return total

packages = []

for name in os.listdir(site_packages):
    path = os.path.join(site_packages, name)
    if os.path.isdir(path):
        size = get_size(path)
        packages.append((name, size))

# 排序
packages.sort(key=lambda x: x[1], reverse=True)

# 统计
total_packages = len(packages)
total_size = sum(size for _, size in packages)

print("=" * 50)
print(f"包总数量: {total_packages}")
print(f"总大小: {total_size / 1024 / 1024:.2f} MB")
print(f"平均大小: {total_size / total_packages / 1024 / 1024:.2f} MB")
print("=" * 50)

print("\nTop 10 最大包:\n")

for name, size in packages[:10]:
    print(f"{name:30} {size / 1024 / 1024:.2f} MB")