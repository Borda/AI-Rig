<!-- file: modality-dispatch.md — consumers: eda.md, training.md, inference.md -->

## Modality-specific sample display — dispatch by `input_modality`

Use wherever samples need showing (EDA Section 3, training sanity check, inference spot check). Pick matching branch; each is self-contained cell set.

## Contents

- `image`: 2D image grids and dimension checks
- `image-3d`: volumetric loading, three-plane viewer, and statistics
- `tabular`: descriptive statistics, correlation, and target plots
- `point-cloud`: bounded 3D point display

______________________________________________________________________

### `image` — 2D images (default)

Setup: no extra installs needed.

```python
# %%
# show_images: grid of N samples with label overlay
def show_images(df, n=9, img_dir=PATH_DATASET):
    assert len(df) >= n, f"Need at least {n} images for the sample grid; found {len(df)}"
    n_cols = 3
    n_rows = (n + n_cols - 1) // n_cols
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(16, 4 * n_rows))
    for ax, (_, row) in zip(axes.flat, df.sample(n).iterrows()):
        img = plt.imread(os.path.join(img_dir, row["image_id"]))  # adapt column name
        ax.imshow(img)
        ax.set_title(str(row.get("label", "")), fontsize=8)
        ax.axis("off")
    plt.tight_layout()
    plt.show()


_ = show_images(df_train)
```

Optional dimension scatter (when image sizes vary):

```python
# %%
from PIL import Image

df_train["w"], df_train["h"] = zip(
    *[Image.open(os.path.join(PATH_DATASET, r["image_id"])).size for _, r in df_train.iterrows()]
)
_ = df_train.plot.scatter("w", "h", alpha=0.3, title="Image dimensions")
```

______________________________________________________________________

### `image-3d` — volumetric / TIFF stacks / medical imaging

Setup cell (add to notebook setup `# %%`):

```python
# %%
# ! pip install -q tifffile imagecodecs ipywidgets
# ! pip list | grep -E 'tifffile|ipywidgets'
```

Imports (add to imports `# %%`):

```python
import tifffile
import ipywidgets as widgets
from ipywidgets import interact, IntSlider, FloatSlider
from matplotlib.colors import ListedColormap, BoundaryNorm
```

Load one volume:

```python
# %%
def load_volume(sample_id):
    img = tifffile.imread(os.path.join(PATH_DATASET, "train_images", f"{sample_id}.tif"))
    mask = tifffile.imread(os.path.join(PATH_DATASET, "train_labels", f"{sample_id}.tif"))
    return img, mask


sample_id = df_train.iloc[0]["id"]  # adapt column name
vol, mask_vol = load_volume(sample_id)
print(f"Volume: {vol.shape} {vol.dtype}  |  Mask: {mask_vol.shape}  labels: {np.unique(mask_vol)}")
```

Interactive 3-plane viewer + mask overlay:

```python
# %%
_MASK_COLORS = ["lightgray", "yellow", "cyan", "red"]
_MASK_CMAP = ListedColormap(_MASK_COLORS[: len(np.unique(mask_vol))])
_MASK_NORM = BoundaryNorm(np.arange(-0.5, len(np.unique(mask_vol))), _MASK_CMAP.N)


def show_volume(vol, mask_vol, z, y, x, mask_alpha):
    vZ, vY, vX = vol.shape[:3]
    fig = plt.figure(figsize=(14, 14))
    ax_xy = fig.add_subplot(2, 2, 1)
    ax_yz = fig.add_subplot(2, 2, 2)
    ax_xz = fig.add_subplot(2, 2, 3)
    ax_3d = fig.add_subplot(2, 2, 4, projection="3d")

    for ax, sl, msl, title in [
        (ax_xy, vol[z, :, :], mask_vol[z, :, :], f"Axial z={z}"),
        (ax_yz, vol[:, :, x], mask_vol[:, :, x], f"Sagittal x={x}"),
        (ax_xz, vol[:, y, :], mask_vol[:, y, :], f"Coronal y={y}"),
    ]:
        ax.imshow(sl, cmap="gray")
        ax.imshow(msl, cmap=_MASK_CMAP, norm=_MASK_NORM, alpha=mask_alpha, interpolation="nearest")
        ax.set_title(title)
        ax.axis("off")

    Yg, Xg = np.meshgrid(np.arange(vY), np.arange(vX), indexing="ij")
    Ys, Zs = np.meshgrid(np.arange(vY), np.arange(vZ), indexing="ij")
    Xc, Zc = np.meshgrid(np.arange(vX), np.arange(vZ), indexing="ij")
    ax_3d.plot_surface(Xg, Yg, np.full_like(Xg, z), color="r", alpha=0.15)
    ax_3d.plot_surface(np.full_like(Ys, x), Ys, Zs, color="b", alpha=0.10)
    ax_3d.plot_surface(Xc, np.full_like(Xc, y), Zc, color="g", alpha=0.10)
    ax_3d.set(xlabel="X", ylabel="Y", zlabel="Z")
    ax_3d.set_title("Slice planes")
    plt.tight_layout()
    plt.show()


interact(
    lambda z, y, x, mask_alpha: show_volume(vol, mask_vol, z, y, x, mask_alpha),
    z=IntSlider(min=0, max=vol.shape[0] - 1, step=1, value=vol.shape[0] // 2, description="Z-slice"),
    y=IntSlider(min=0, max=vol.shape[1] - 1, step=1, value=vol.shape[1] // 2, description="Y-slice"),
    x=IntSlider(min=0, max=vol.shape[2] - 1, step=1, value=vol.shape[2] // 2, description="X-slice"),
    mask_alpha=FloatSlider(min=0.0, max=1.0, step=0.05, value=0.3, description="Mask α"),
)
```

Volume statistics:

```python
# %%
print(f"Volume: min={vol.min()}, max={vol.max()}, mean={vol.mean():.2f}")
label_counts = dict(zip(*np.unique(mask_vol, return_counts=True)))
print(f"Mask labels: {label_counts}")
_ = pd.Series(label_counts).plot(kind="bar", title="Label voxel counts")
```

______________________________________________________________________

### `tabular` — structured / CSV only

Setup: no extra installs beyond pandas.

```python
# %%
# shape, dtypes, missing values
print(df_train.shape)
display(df_train.describe())
print(df_train.isnull().sum().sort_values(ascending=False).head(20))
```

```python
# %%
import seaborn as sns

num_cols = df_train.select_dtypes("number").columns.tolist()
assert 0 < len(num_cols) <= 20, "Feature-correlation chart requires 1–20 numeric columns"
fig, ax = plt.subplots(figsize=(len(num_cols), len(num_cols)))
sns.heatmap(df_train[num_cols].corr(), annot=True, fmt=".2f", ax=ax)
ax.set_title("Feature correlation")
plt.tight_layout()
plt.show()
```

```python
# %%
# Target distribution
_ = df_train[TARGET_COL].value_counts().plot(kind="bar", title="Target distribution")
```

______________________________________________________________________

### `point-cloud` — 3D point sets

Setup cell:

```python
# %%
# ! pip install -q open3d
# ! python -c "import open3d; print(open3d.__version__)"
```

```python
# %%
import open3d as o3d


def show_pcd(path, n_points=50_000):
    pcd = o3d.io.read_point_cloud(str(path))
    pts = np.asarray(pcd.points)[:n_points]
    fig = plt.figure(figsize=(10, 7))
    ax = fig.add_subplot(111, projection="3d")
    ax.scatter(pts[:, 0], pts[:, 1], pts[:, 2], s=0.1, alpha=0.5)
    ax.set_title(str(path))
    plt.tight_layout()
    plt.show()


point_paths = sorted(Path(PATH_DATASET).glob("**/*.pcd"))
assert point_paths, "No point-cloud files found for the required sample display"
sample_path = point_paths[0]
show_pcd(sample_path)
```
