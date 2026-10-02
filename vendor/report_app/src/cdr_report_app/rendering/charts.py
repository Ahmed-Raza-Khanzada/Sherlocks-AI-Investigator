"""Chart generation for the PDF renderer."""

from __future__ import annotations

import gc
import math
import os
import tempfile

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

from cdr_report_app.domain.analysis_models import DailyActivityAnalysis, HeatmapAnalysis, MovementStep


def create_daily_activity_chart(activity: DailyActivityAnalysis) -> str | None:
    if not activity.bins:
        return None

    labels = [item.label for item in activity.bins]
    values = [item.value for item in activity.bins]
    colors = [item.color or "#667eea" for item in activity.bins]
    groups = []
    for item in activity.bins:
        if item.group and item.group not in groups:
            groups.append(item.group)

    fig, ax = plt.subplots(figsize=(7, 3.5))
    bars = ax.bar(range(len(labels)), values, color=colors, alpha=0.88, width=0.7, edgecolor="white", linewidth=0.5)

    for bar_obj, val in zip(bars, values):
        if val > 0:
            ax.text(bar_obj.get_x() + bar_obj.get_width() / 2, bar_obj.get_height() + 0.3, str(int(val)), ha="center", va="bottom", fontsize=6, fontweight="bold", color="#333333")

    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, fontsize=5.5, rotation=45, ha="right")
    ax.set_ylabel("Calls / SMS", fontsize=8, fontweight="bold")
    if activity.title_suffix:
        ax.set_title(activity.title_suffix, fontsize=10, fontweight="bold", pad=15, color="#1a1a1a")

    if groups:
        n_per_day = max(1, len(labels) // len(groups))
        for i, name in enumerate(groups):
            if i > 0:
                ax.axvline(x=i * n_per_day - 0.5, color="#cccccc", linestyle="--", linewidth=0.8, alpha=0.7)
            start_x = i * n_per_day
            end_x = min(len(labels) - 1, (i + 1) * n_per_day - 1)
            center_x = (start_x + end_x) / 2
            ax.text(center_x, -0.22, name, transform=ax.get_xaxis_transform(), ha="center", va="top", fontsize=8, fontweight="bold", color="#1a1a1a")

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#cccccc")
    ax.spines["bottom"].set_color("#cccccc")
    ax.tick_params(axis="y", labelsize=7)
    ax.tick_params(axis="x", pad=6)
    ax.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    ax.grid(axis="y", alpha=0.3, linestyle="--", color="#cccccc")

    plt.tight_layout()
    plt.subplots_adjust(bottom=0.25)
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    fig.savefig(tmp.name, dpi=150, bbox_inches="tight", transparent=False, facecolor="white")
    plt.close(fig)
    del fig
    gc.collect()
    return tmp.name


def create_heatmap_chart(heatmap: HeatmapAnalysis) -> str | None:
    if not heatmap.matrix or not heatmap.day_labels or not heatmap.hour_labels:
        return None

    import numpy as np

    data = np.array(heatmap.matrix, dtype=float)
    fig, ax = plt.subplots(figsize=(8.5, 5.2), facecolor="white")
    mesh = ax.pcolormesh(data, cmap=plt.cm.YlOrRd, edgecolors="#e0e0e0", linewidth=0.5, shading="auto")
    cbar = fig.colorbar(mesh, ax=ax, shrink=0.85, pad=0.04, fraction=0.046)
    cbar.ax.tick_params(labelsize=7)
    cbar.set_label("Calls / SMS", fontsize=8, fontweight="bold")

    ax.set_xticks([i + 0.5 for i in range(len(heatmap.day_labels))])
    ax.set_xticklabels(heatmap.day_labels, fontsize=8, fontweight="bold", color="#1a1a1a")
    ax.set_xlabel("Day of Week", fontsize=8, fontweight="bold", color="#333333", labelpad=10)

    ax.set_yticks([i + 0.5 for i in range(len(heatmap.hour_labels))])
    ax.set_yticklabels(heatmap.hour_labels, fontsize=7, color="#1a1a1a")
    ax.set_ylabel("Hour of Day", fontsize=8, fontweight="bold", color="#333333", labelpad=10)
    ax.invert_yaxis()

    ax.set_xticks([i for i in range(len(heatmap.day_labels) + 1)], minor=True)
    ax.set_yticks([i for i in range(len(heatmap.hour_labels) + 1)], minor=True)
    ax.grid(which="minor", color="#cccccc", linestyle="-", linewidth=0.5, alpha=0.5)
    ax.tick_params(which="major", length=0)

    max_val = data.max() if data.max() > 0 else 1
    for i in range(data.shape[0]):
        for j in range(data.shape[1]):
            val = int(data[i, j])
            if val > 0:
                text_clr = "white" if (data[i, j] / max_val) > 0.45 else "#0a0a0a"
                ax.text(j + 0.5, i + 0.5, str(val), ha="center", va="center", fontsize=6.5, color=text_clr, fontweight="bold", family="monospace")

    ax.set_title("Hourly Activity by Day of Week", fontsize=11, fontweight="bold", pad=12, color="#1a1a1a")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#cccccc")
    ax.spines["bottom"].set_color("#cccccc")

    plt.tight_layout()
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    fig.savefig(tmp.name, dpi=150, bbox_inches="tight", transparent=False, facecolor="white")
    plt.close(fig)
    del data, fig
    gc.collect()
    return tmp.name


def create_movement_map(steps: list[MovementStep], crime_lat: str | None = None, crime_lng: str | None = None) -> str | None:
    locations = [
        {
            "s_no": step.order,
            "lat": step.latitude,
            "lng": step.longitude,
        }
        for step in steps
        if step.latitude is not None and step.longitude is not None
    ]
    if not locations and not (crime_lat and crime_lng):
        return None
    return _create_spatial_map(locations, crime_lat, crime_lng, connect_sequential=True)


def _create_number_icon(number: int | str, color: str = "#4facfe"):
    from PIL import Image, ImageDraw, ImageFont

    size = 40
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse([2, 2, size - 3, size - 3], fill=color, outline="white", width=2)
    try:
        font = ImageFont.load_default()
        text = str(number)
        if hasattr(draw, "textbbox"):
            bbox = draw.textbbox((0, 0), text, font=font)
            text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        else:
            text_w, text_h = draw.textsize(text, font=font)
        draw.text(((size - text_w) / 2, (size - text_h) / 2 - 1), text, fill="white", font=font)
    except Exception:
        pass
    return image


def _create_crime_callout_icon():
    from PIL import Image, ImageDraw, ImageFont

    width, height = 118, 28
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([2, 3, width - 2, height - 3], radius=8, fill=(255, 255, 255, 240), outline=(220, 53, 69, 255), width=2)

    try:
        font = ImageFont.load_default()
        text = "Crime location"
        if hasattr(draw, "textbbox"):
            bbox = draw.textbbox((0, 0), text, font=font)
            text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        else:
            text_w, text_h = draw.textsize(text, font=font)
        draw.text((10, (height - text_h) / 2 - 1), text, fill=(220, 53, 69, 255), font=font)
    except Exception:
        pass

    return image


def _create_spatial_map(locations: list[dict[str, object]], clat: str | float | None = None, clng: str | float | None = None, connect_sequential: bool = False) -> str | None:
    try:
        if clat is not None and str(clat).strip() != "-":
            clat = float(clat)
        else:
            clat = None
        if clng is not None and str(clng).strip() != "-":
            clng = float(clng)
        else:
            clng = None
    except (TypeError, ValueError):
        clat, clng = None, None

    try:
        from staticmap import CircleMarker, IconMarker, Line, StaticMap

        static_map = StaticMap(600, 400)
        plotted_locs = 0
        icon_paths: list[str] = []
        path_points: list[tuple[float, float]] = []
        location_counts: dict[tuple[float, float], int] = {}

        def get_jittered(lat: object, lng: object) -> tuple[float, float]:
            base_lat, base_lng = float(lat), float(lng)
            base_key = (round(base_lat, 7), round(base_lng, 7))
            idx = location_counts.get(base_key, 0)
            location_counts[base_key] = idx + 1

            if idx > 0:
                angle = idx * (math.pi / 3.0)
                radius = 0.00028 + (idx // 6) * 0.00010
                jitter_lat = base_lat + math.sin(angle) * radius
                jitter_lng = base_lng + math.cos(angle) * radius
            else:
                jitter_lat, jitter_lng = base_lat, base_lng

            if clat is not None and clng is not None:
                crime_gap = ((jitter_lat - clat) ** 2 + (jitter_lng - clng) ** 2) ** 0.5
                if crime_gap < 2.2e-4:
                    jitter_lat += 0.00018
                    jitter_lng -= 0.00018
            return jitter_lat, jitter_lng

        def crime_callout() -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
            coords = list(path_points) if path_points else [(clng, clat)]
            xs = [point[0] for point in coords]
            ys = [point[1] for point in coords]
            lng_span = max(xs) - min(xs) if len(xs) > 1 else 0.0
            lat_span = max(ys) - min(ys) if len(ys) > 1 else 0.0
            dx = max(lng_span * 0.16, 0.0010)
            dy = max(lat_span * 0.14, 0.0008)
            start = (clng + dx, clat + dy)
            ux, uy = -dx, -dy
            dist = (ux**2 + uy**2) ** 0.5 or 1.0
            ux, uy = ux / dist, uy / dist
            px, py = -uy, ux
            head = max(min(dist * 0.22, 0.00055), 0.00028)
            base_x = clng - ux * head
            base_y = clat - uy * head
            left = (base_x + px * head * 0.55, base_y + py * head * 0.55)
            right = (base_x - px * head * 0.55, base_y - py * head * 0.55)
            return start, left, right

        for index, loc in enumerate(locations):
            lat_val = loc.get("lat")
            lng_val = loc.get("lng")
            if lat_val is None or lng_val is None:
                continue
            jitter_lat, jitter_lng = get_jittered(lat_val, lng_val)
            path_points.append((jitter_lng, jitter_lat))

            label = str(loc.get("s_no", index + 1))
            icon_image = _create_number_icon(label)
            icon_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            icon_image.save(icon_tmp.name)
            icon_paths.append(icon_tmp.name)

            static_map.add_marker(IconMarker((jitter_lng, jitter_lat), icon_tmp.name, 20, 20))
            plotted_locs += 1

            if not connect_sequential and clat is not None and clng is not None:
                static_map.add_line(Line(((jitter_lng, jitter_lat), (clng, clat)), "#999999", 1))

        if connect_sequential and len(path_points) > 1:
            static_map.add_line(Line(tuple(path_points), "#4facfe", 3))
            for start, end in zip(path_points[:-1], path_points[1:]):
                x1, y1 = start
                x2, y2 = end
                dx, dy = x2 - x1, y2 - y1
                dist = (dx**2 + dy**2) ** 0.5
                if dist <= 0.0001:
                    continue
                ux, uy = dx / dist, dy / dist
                px, py = -uy, ux
                size = 0.0006
                mx, my = x1 + dx * 0.7, y1 + dy * 0.7
                p1 = (mx - ux * size + px * size * 0.6, my - uy * size + py * size * 0.6)
                p2 = (mx - ux * size - px * size * 0.6, my - uy * size - py * size * 0.6)
                static_map.add_line(Line([p1, (mx, my), p2], "#4facfe", 3))

        if clat is not None and clng is not None:
            callout_start, arrow_left, arrow_right = crime_callout()
            static_map.add_line(Line((callout_start, (clng, clat)), "#dc3545", 3))
            static_map.add_line(Line((arrow_left, (clng, clat), arrow_right), "#dc3545", 3))
            static_map.add_marker(CircleMarker(callout_start, "#ffffff", 9))
            static_map.add_marker(CircleMarker(callout_start, "#dc3545", 5))
            callout_icon = _create_crime_callout_icon()
            callout_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            callout_icon.save(callout_tmp.name)
            icon_paths.append(callout_tmp.name)
            static_map.add_marker(IconMarker((callout_start[0] + 0.00175, callout_start[1]), callout_tmp.name, 92, 22))

        if plotted_locs > 0 or (clat is not None and clng is not None):
            image = static_map.render()
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            image.save(tmp.name)
            for path in icon_paths:
                try:
                    os.remove(path)
                except OSError:
                    pass
            return tmp.name
    except Exception:
        pass

    fig, ax = plt.subplots(figsize=(6.2, 4.2), facecolor="white")
    ax.set_facecolor("#eef4f7")
    path_points: list[tuple[float, float]] = []
    location_counts: dict[tuple[float, float], int] = {}

    for index, loc in enumerate(locations):
        lat_val = loc.get("lat")
        lng_val = loc.get("lng")
        if lat_val is None or lng_val is None:
            continue
        try:
            base_lat, base_lng = float(lat_val), float(lng_val)
        except (TypeError, ValueError):
            continue

        base_key = (round(base_lat, 7), round(base_lng, 7))
        idx = location_counts.get(base_key, 0)
        location_counts[base_key] = idx + 1

        if idx > 0:
            angle = idx * (math.pi / 3.0)
            radius = 0.00028 + (idx // 6) * 0.00010
            jitter_lat = base_lat + math.sin(angle) * radius
            jitter_lng = base_lng + math.cos(angle) * radius
        else:
            jitter_lat, jitter_lng = base_lat, base_lng

        if clat is not None and clng is not None:
            crime_gap = ((jitter_lat - clat) ** 2 + (jitter_lng - clng) ** 2) ** 0.5
            if crime_gap < 2.2e-4:
                jitter_lat += 0.00018
                jitter_lng -= 0.00018

        ax.scatter(jitter_lng, jitter_lat, color="#4facfe", s=180, edgecolors="white", linewidth=1.5, zorder=4)
        ax.text(jitter_lng, jitter_lat, str(loc.get("s_no", index + 1)), fontsize=8, fontweight="bold", ha="center", va="center", color="white", zorder=5)
        path_points.append((jitter_lng, jitter_lat))
        if not connect_sequential and clat is not None and clng is not None:
            ax.plot([jitter_lng, clng], [jitter_lat, clat], color="#9aa8b6", linestyle="--", linewidth=1.0, alpha=0.6, zorder=2)

    if connect_sequential and len(path_points) > 1:
        px, py = zip(*path_points)
        ax.plot(px, py, color="#4facfe", linewidth=3, zorder=2)
        for start, end in zip(path_points[:-1], path_points[1:]):
            ax.annotate(
                "",
                xy=(end[0], end[1]),
                xytext=(start[0], start[1]),
                arrowprops={"arrowstyle": "->", "color": "#4facfe", "lw": 2, "mutation_scale": 15},
            )

    if clat is not None and clng is not None:
        coords = list(path_points) if path_points else [(clng, clat)]
        xs = [point[0] for point in coords]
        ys = [point[1] for point in coords]
        lng_span = max(xs) - min(xs) if len(xs) > 1 else 0.0
        lat_span = max(ys) - min(ys) if len(ys) > 1 else 0.0
        dx = max(lng_span * 0.16, 0.0010)
        dy = max(lat_span * 0.14, 0.0008)
        start_x, start_y = clng + dx, clat + dy
        ax.annotate(
            "",
            xy=(clng, clat),
            xytext=(start_x, start_y),
            arrowprops={"arrowstyle": "->", "color": "#dc3545", "lw": 2.2, "mutation_scale": 14},
            zorder=6,
        )
        ax.scatter(start_x, start_y, color="#ffffff", s=46, edgecolors="#dc3545", linewidth=1.2, zorder=9)
        ax.scatter(start_x, start_y, color="#dc3545", s=10, edgecolors="none", zorder=10)
        ax.text(
            start_x + 0.00028,
            start_y,
            "Crime location",
            fontsize=7,
            fontweight="bold",
            color="#dc3545",
            va="center",
            ha="left",
            bbox={"facecolor": "white", "edgecolor": "#dc3545", "boxstyle": "round,pad=0.2"},
            zorder=11,
        )

    if path_points or clat is not None:
        xs = [point[0] for point in path_points]
        ys = [point[1] for point in path_points]
        if clng is not None and clat is not None:
            xs.append(clng)
            ys.append(clat)
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        pad_x = max((max_x - min_x) * 0.18, 0.0020)
        pad_y = max((max_y - min_y) * 0.18, 0.0016)
        ax.set_xlim(min_x - pad_x, max_x + pad_x)
        ax.set_ylim(min_y - pad_y, max_y + pad_y)

        grid_x = [min_x - pad_x + ((max_x - min_x + 2 * pad_x) * step / 4) for step in range(5)]
        grid_y = [min_y - pad_y + ((max_y - min_y + 2 * pad_y) * step / 4) for step in range(5)]
        for gx in grid_x:
            ax.axvline(gx, color="#dbe4ea", linewidth=0.8, zorder=0)
        for gy in grid_y:
            ax.axhline(gy, color="#dbe4ea", linewidth=0.8, zorder=0)
    else:
        ax.text(0.5, 0.5, "Map data unavailable", ha="center", va="center", transform=ax.transAxes, fontsize=11, fontweight="bold", color="#6c757d")

    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_title("Movement Static Map", fontsize=11, fontweight="bold", color="#1a2f4b", pad=10)

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    plt.savefig(tmp.name, bbox_inches="tight", dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)
    del fig
    gc.collect()
    return tmp.name


def create_interaction_heatmap(interactions: list, target_ids: list[str]) -> str | None:
    """Create a clean Target vs Target interaction frequency heatmap."""
    if not target_ids:
        return None

    import numpy as np
    import pandas as pd
    import seaborn as sns

    # Build a square matrix
    df_matrix = pd.DataFrame(0, index=target_ids, columns=target_ids)
    for ix in interactions:
        # Since it's undirected, we populate both sides
        s, t = ix.source_identifier, ix.target_identifier
        if s in df_matrix.index and t in df_matrix.columns:
            df_matrix.at[s, t] = ix.interaction_count
            df_matrix.at[t, s] = ix.interaction_count

    fig, ax = plt.subplots(figsize=(8, 6), facecolor="white")
    sns.heatmap(
        df_matrix, 
        annot=True, 
        fmt="d", 
        cmap="YlGnBu", 
        ax=ax, 
        cbar_kws={'label': 'Interaction Count'},
        linewidths=.5,
        square=True
    )
    
    ax.set_title("Target Interaction Matrix", fontsize=12, fontweight="bold", pad=20)
    ax.set_xlabel("Target Identifier", fontsize=10)
    ax.set_ylabel("Target Identifier", fontsize=10)
    plt.xticks(rotation=45, ha="right")

    plt.tight_layout()
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    fig.savefig(tmp.name, dpi=150, bbox_inches="tight")
    plt.close(fig)
    del df_matrix, fig
    gc.collect()
    return tmp.name


def create_event_timeline(events: list, target_ids: list[str]) -> str | None:
    """Create a chronological scatter timeline for calls, co-locations, and crime scene presence."""
    if not events or not target_ids:
        return None

    import pandas as pd
    from datetime import datetime
    import matplotlib.dates as mdates

    data = []
    for ev in events:
        try:
            ts = datetime.strptime(ev.timestamp, "%Y-%m-%d %H:%M:%S")
            targets_involved = list(getattr(ev, "targets", []) or [])
            if not targets_involved:
                targets_involved = [ev.target_1]
                if ev.target_2:
                    targets_involved.append(ev.target_2)
            
            for t in targets_involved:
                if t in target_ids:
                    data.append({
                        "Time": ts,
                        "Target": t,
                        "Type": ev.event_type,
                        "Description": ev.description
                    })
        except Exception:
            continue

    if not data:
        return None

    df = pd.DataFrame(data)
    # Sort by target index to keep Y-axis consistent
    target_order = {tid: i for i, tid in enumerate(target_ids)}
    df['TargetIdx'] = df['Target'].map(target_order)
    df = df.sort_values(['Time', 'TargetIdx'])

    fig, ax = plt.subplots(figsize=(10, 5), facecolor="white")
    
    # Event styling
    styles = {
        "Call/SMS": {"color": "#4facfe", "marker": "o", "label": "Communication"},
        "Co-location": {"color": "#ffa500", "marker": "X", "label": "Co-location"},
        "Crime Scene": {"color": "#dc3545", "marker": "D", "label": "Crime Scene"}
    }
    
    # Plot each type
    for etype, style in styles.items():
        subset = df[df["Type"] == etype]
        if not subset.empty:
            ax.scatter(
                subset["Time"], 
                subset["Target"], 
                label=style["label"],
                color=style["color"],
                marker=style["marker"],
                s=120,
                alpha=0.75,
                edgecolors="white",
                linewidth=1,
                zorder=3
            )

    # Aesthetics
    ax.set_title("Chronological Activity Timeline (Crime Date)", fontsize=13, fontweight="bold", pad=20)
    ax.set_xlabel("Time of Day", fontsize=10, labelpad=10)
    ax.set_ylabel("Suspect Target", fontsize=10)
    
    # Clean grid
    ax.grid(True, linestyle="--", alpha=0.4, axis='x', zorder=0)
    ax.set_axisbelow(True)
    
    # Format X axis for times
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    plt.xticks(rotation=0)
    
    # Add target swimlanes
    for tid in target_ids:
        ax.axhline(y=tid, color="#f0f0f0", linestyle="-", linewidth=0.8, zorder=1)

    ax.legend(loc="upper left", bbox_to_anchor=(1, 1), fontsize=9)
    plt.tight_layout()

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    fig.savefig(tmp.name, dpi=150, bbox_inches="tight")
    plt.close(fig)
    del df, fig
    gc.collect()
    return tmp.name


def create_network_graph(
    common_contacts: list,
    direct_interactions: list,
    target_ids: list[str],
    max_contacts: int = 15,
) -> str | None:
    """Create a network graph showing targets and their shared contacts/interactions."""
    if not target_ids:
        return None

    try:
        import networkx as nx
    except ImportError:
        return None

    G = nx.Graph()

    # Add target nodes
    for tid in target_ids:
        G.add_node(tid, node_type="target")

    # Add direct interactions (Target↔Target edges)
    for ix in direct_interactions:
        s, t = ix.source_identifier, ix.target_identifier
        if s in target_ids and t in target_ids:
            G.add_edge(s, t, weight=ix.interaction_count, edge_type="direct")

    # Add top common contacts as intermediate nodes
    sorted_contacts = sorted(common_contacts, key=lambda c: (len(c.target_identifiers), c.total_interactions), reverse=True)
    for cc in sorted_contacts[:max_contacts]:
        short_num = cc.contact_number[-7:] if len(cc.contact_number) > 7 else cc.contact_number
        node_id = f"...{short_num}"
        G.add_node(node_id, node_type="contact")
        for tid in cc.target_identifiers:
            if tid in target_ids:
                G.add_edge(tid, node_id, weight=max(1, cc.total_interactions // max(1, len(cc.target_identifiers))), edge_type="shared")

    if G.number_of_nodes() < 2:
        return None

    fig, ax = plt.subplots(figsize=(10, 7), facecolor="white")

    pos = nx.spring_layout(G, k=2.5, iterations=60, seed=42)

    # Draw edges
    direct_edges = [(u, v) for u, v, d in G.edges(data=True) if d.get("edge_type") == "direct"]
    shared_edges = [(u, v) for u, v, d in G.edges(data=True) if d.get("edge_type") == "shared"]

    if direct_edges:
        direct_weights = [G[u][v]["weight"] for u, v in direct_edges]
        max_w = max(direct_weights) if direct_weights else 1
        direct_widths = [max(1.5, (w / max_w) * 5) for w in direct_weights]
        nx.draw_networkx_edges(G, pos, edgelist=direct_edges, width=direct_widths, edge_color="#dc3545", alpha=0.7, ax=ax)

    if shared_edges:
        nx.draw_networkx_edges(G, pos, edgelist=shared_edges, width=1.0, edge_color="#aaaaaa", style="dashed", alpha=0.5, ax=ax)

    # Draw nodes
    target_nodes = [n for n, d in G.nodes(data=True) if d.get("node_type") == "target"]
    contact_nodes = [n for n, d in G.nodes(data=True) if d.get("node_type") == "contact"]

    nx.draw_networkx_nodes(G, pos, nodelist=target_nodes, node_size=900, node_color="#29417A", edgecolors="white", linewidths=2, ax=ax)
    nx.draw_networkx_nodes(G, pos, nodelist=contact_nodes, node_size=250, node_color="#FFC107", edgecolors="#aaa", linewidths=0.5, ax=ax)

    # Labels
    target_labels = {n: n.split("(")[0].strip() if "(" in n else n for n in target_nodes}
    nx.draw_networkx_labels(G, pos, labels=target_labels, font_size=7, font_weight="bold", font_color="white", ax=ax)
    contact_labels = {n: n for n in contact_nodes}
    nx.draw_networkx_labels(G, pos, labels=contact_labels, font_size=5.5, font_color="#333", ax=ax)

    # Edge weight labels for direct interactions
    if direct_edges:
        edge_labels = {(u, v): str(G[u][v]["weight"]) for u, v in direct_edges if G[u][v]["weight"] > 0}
        nx.draw_networkx_edge_labels(G, pos, edge_labels=edge_labels, font_size=7, font_color="#dc3545", ax=ax)

    ax.set_title("Suspect Network Graph", fontsize=13, fontweight="bold", pad=20, color="#1a1a1a")

    # Legend
    from matplotlib.lines import Line2D
    legend_elements = [
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#29417A", markersize=12, label="Target/Suspect"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#FFC107", markersize=8, label="Common Contact"),
        Line2D([0], [0], color="#dc3545", linewidth=2, label="Direct Call"),
        Line2D([0], [0], color="#aaaaaa", linewidth=1, linestyle="--", label="Shared Contact"),
    ]
    ax.legend(handles=legend_elements, loc="upper left", fontsize=8, framealpha=0.9)

    ax.axis("off")
    plt.tight_layout()

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    fig.savefig(tmp.name, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    del G, pos, fig
    gc.collect()
    return tmp.name


def create_hourly_density_bar(density) -> str | None:
    """Bar chart of hourly call counts with spike bars highlighted in red.

    *density* is a `HourlyDensity` model.  Returns a temp-file path or None.
    """
    if not density or not density.buckets:
        return None

    hours = [b.hour for b in density.buckets]
    counts = [b.call_count for b in density.buckets]
    colors = ["#C00000" if b.is_spike else "#29417A" for b in density.buckets]

    fig, ax = plt.subplots(figsize=(7, 3))
    bars = ax.bar(hours, counts, color=colors, alpha=0.88, width=0.7, edgecolor="white", linewidth=0.5)

    for bar_obj, val in zip(bars, counts):
        if val > 0:
            ax.text(
                bar_obj.get_x() + bar_obj.get_width() / 2,
                bar_obj.get_height() + 0.5,
                str(int(val)),
                ha="center", va="bottom", fontsize=6, fontweight="bold", color="#333333",
            )

    if density.spike_threshold > 0:
        ax.axhline(y=density.spike_threshold, color="#C00000", linestyle="--", linewidth=0.8, alpha=0.7, label=f"Spike threshold ({density.spike_threshold:.0f})")
        ax.legend(fontsize=7, loc="upper right")

    ax.set_xticks(range(24))
    ax.set_xticklabels([str(h) for h in range(24)], fontsize=6)
    ax.set_xlabel("Hour of Day", fontsize=8)
    ax.set_ylabel("Call Count", fontsize=8)
    ax.set_title("Hourly Call Density", fontsize=9, fontweight="bold", color="#1a1a1a")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#cccccc")
    ax.spines["bottom"].set_color("#cccccc")
    ax.tick_params(axis="y", labelsize=7)
    ax.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    ax.grid(axis="y", alpha=0.3, linestyle="--", color="#cccccc")

    plt.tight_layout()
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    fig.savefig(tmp.name, dpi=150, bbox_inches="tight", transparent=False, facecolor="white")
    plt.close(fig)
    del fig
    gc.collect()
    return tmp.name
