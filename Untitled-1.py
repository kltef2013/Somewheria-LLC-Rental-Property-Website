from build123d import *

# --- Global Rendering Quality ---
# build123d handles smoothness automatically based on geometry,
# but can be influenced by the `AngularTolerance` for faces.
# No direct equivalent to OpenSCAD's $fn, but high quality by default.

# --- Main Body Dimensions ---
body_length = 60  # X-axis dimension (front to back / longest side)
body_width = 40   # Y-axis dimension (left to right / shorter side)
body_height = 20  # Z-axis dimension (top to bottom)
corner_radius = 3 # Radius for rounding all outer edges of the main body

# --- Top Recess Dimensions (useful for holding small screws/parts) ---
recess_inset_x = 5  # Distance from the front/back edges of the body to the recess edge (X-axis)
recess_inset_y = 5  # Distance from the left/right edges of the body to the recess edge (Y-axis)
recess_depth = 4    # How deep the recess is cut into the body from the top surface
recess_inner_radius = 2 # Radius for rounding the inner corners of the recess

# Calculate the absolute dimensions of the recess for clarity
recess_actual_length = body_length - 2 * recess_inset_x
recess_actual_width = body_width - 2 * recess_inset_y
# Calculate the Z-coordinate for the top surface of the recess
recess_top_z_level = body_height - recess_depth

# --- PC Specific Bit Hole & Tool Slot Nominal Dimensions ---
# These are the *ideal* (nominal) sizes of the actual bits/tools you want to hold.
pc_hex_bit_diameter_small_nominal = 3.5
pc_hex_bit_diameter_medium_nominal = 5.0

pc_tool_slot_length = 30  # Common length for the rectangular tool slots
pc_bit_hole_depth = 15    # Total depth for all bit holes and tool slots from the recess top surface

# --- Retention parameters for holes and slots (these define the 'two-stage' fit) ---
bit_entry_flare_tolerance = 0.5   # Extra diameter for the wide opening (flare)
bit_entry_flare_depth = 4         # Depth of the wide opening (flare)
bit_retention_snugness = 0.1      # Amount to *reduce* diameter from nominal for the tight friction fit

tool_slot_entry_flare_tolerance = 0.4 # Extra width for the wide opening (flare) of tool slots
tool_slot_entry_flare_depth = 4       # Depth of the wide opening (flare) for tool slots
tool_slot_retention_snugness = 0.2  # Amount to *reduce* width from nominal for the tight friction fit

# --- Nominal Widths for Specialized PC Tools ---
pc_tweezer_slot_width_nominal = 3.0
pc_spudger_slot_width_nominal = 5.0

# --- Positions for Rectangular Tool Slots (Tweezers & Spudger) ---
# All positions are absolute coordinates relative to the body's origin [0,0,0].
slot_start_x = recess_inset_x + 8
tweezer_slot_start_y = recess_inset_y + 6
spudger_slot_start_y = tweezer_slot_start_y + pc_tweezer_slot_width_nominal + 8

# --- Positions for Circular Bit Holes (Hex Bits) ---
circ_hole_center_x = (recess_inset_x + recess_actual_length) - 8
circ_hole_top_y = recess_inset_y + 6 + (pc_hex_bit_diameter_small_nominal / 2)
circ_hole_middle_y = circ_hole_top_y + (pc_hex_bit_diameter_small_nominal / 2) + (pc_hex_bit_diameter_medium_nominal / 2) + 8
circ_hole_bottom_y = circ_hole_middle_y + (pc_hex_bit_diameter_medium_nominal / 2) + (pc_hex_bit_diameter_small_nominal / 2) + 8

# --- Small Center Circular Hole Dimensions ---
small_center_hole_diameter = 4.2
small_center_hole_depth = 10
center_recess_x = recess_inset_x + (recess_actual_length / 2)
center_recess_y = recess_inset_y + (recess_actual_width / 2)

# --- Clip Dimensions ---
clip_thickness = 3
clip_width = 15
clip_projection_length = 35
clip_tip_clearance = 2

# --- Modules / Functions for Reusable Shapes ---

def create_main_body() -> Solid:
    """Creates the main body of the tool holder with all edges rounded."""
    # Build123d's Box can directly take a fillet radius for all edges
    return Box(
        body_length, body_width, body_height,
        align=(Align.MIN, Align.MIN, Align.MIN) # Align to (0,0,0) corner
    ).fillet_all_edges(corner_radius)

def create_top_recess_cutout() -> Solid:
    """Creates the negative space (cutout) for the top recess."""
    recess = Box(
        recess_actual_length,
        recess_actual_width,
        recess_depth + 0.2, # Slightly extra depth to ensure clean cut
        align=(Align.MIN, Align.MIN, Align.MIN)
    ).fillet_all_edges(recess_inner_radius)
    # Position the recess just above the top surface of the main body, aligned for cutting
    return recess.locate(
        Location((recess_inset_x, recess_inset_y, recess_top_z_level - 0.1))
    )

def secure_bit_hole(diameter_nominal: float, total_depth: float, flare_tolerance: float, flare_depth: float, retention_snugness: float) -> Solid:
    """
    Generates a cylindrical cutout with a two-stage diameter for secure retention.
    """
    with Compound() as hole_cutout:
        # Top section: wider for easy bit entry
        Cylinder(
            radius=(diameter_nominal + flare_tolerance) / 2,
            height=flare_depth + 0.1,
            align=(Align.CENTER, Align.CENTER, Align.MIN) # Align base for stacking
        )
        # Bottom section: narrower for friction fit
        Cylinder(
            radius=(diameter_nominal - retention_snugness) / 2,
            height=total_depth - flare_depth + 0.1,
            align=(Align.CENTER, Align.CENTER, Align.MIN)
        ).locate(Location((0, 0, flare_depth)))
    return hole_cutout.fuse() # Fuse the two cylinders into one solid for cutting

def secure_rectangular_slot(width_nominal: float, length: float, total_depth: float, flare_tolerance: float, flare_depth: float, retention_snugness: float) -> Solid:
    """
    Generates a rectangular cutout with a two-stage width for secure retention.
    """
    with Compound() as slot_cutout:
        # Top section: wider for easy tool entry
        Box(
            length,
            width_nominal + flare_tolerance,
            flare_depth + 0.1,
            align=(Align.MIN, Align.MIN, Align.MIN)
        )
        # Bottom section: narrower for friction fit
        Box(
            length,
            width_nominal - retention_snugness,
            total_depth - flare_depth + 0.1,
            align=(Align.MIN, Align.MIN, Align.MIN)
        ).locate(Location((0, 0, flare_depth)))
    return slot_cutout.fuse() # Fuse the two boxes into one solid for cutting

def create_rectangular_tool_slots_cutouts() -> Compound:
    """Places the individual secure rectangular slots within the tool holder."""
    slot_cut_z_level = recess_top_z_level - pc_bit_hole_depth - 0.1 # Ensure clean cut

    with Compound() as tool_slots:
        # Tweezer Slot
        secure_rectangular_slot(
            pc_tweezer_slot_width_nominal,
            pc_tool_slot_length,
            pc_bit_hole_depth,
            tool_slot_entry_flare_tolerance,
            tool_slot_entry_flare_depth,
            tool_slot_retention_snugness
        ).locate(Location((slot_start_x, tweezer_slot_start_y, slot_cut_z_level)))

        # Spudger Slot
        secure_rectangular_slot(
            pc_spudger_slot_width_nominal,
            pc_tool_slot_length,
            pc_bit_hole_depth,
            tool_slot_entry_flare_tolerance,
            tool_slot_entry_flare_depth,
            tool_slot_retention_snugness
        ).locate(Location((slot_start_x, spudger_slot_start_y, slot_cut_z_level)))
    return tool_slots

def create_circular_bit_holes_cutouts() -> Compound:
    """Places the individual secure circular bit holes."""
    hole_cut_z_level = recess_top_z_level - pc_bit_hole_depth - 0.1 # Ensure clean cut

    with Compound() as bit_holes:
        # Hole 1 (top-right: small hex bit)
        secure_bit_hole(
            pc_hex_bit_diameter_small_nominal,
            pc_bit_hole_depth,
            bit_entry_flare_tolerance,
            bit_entry_flare_depth,
            bit_retention_snugness
        ).locate(Location((circ_hole_center_x, circ_hole_top_y, hole_cut_z_level)))

        # Hole 2 (middle-right: medium hex bit)
        secure_bit_hole(
            pc_hex_bit_diameter_medium_nominal,
            pc_bit_hole_depth,
            bit_entry_flare_tolerance,
            bit_entry_flare_depth,
            bit_retention_snugness
        ).locate(Location((circ_hole_center_x, circ_hole_middle_y, hole_cut_z_level)))

        # Hole 3 (bottom-right: small hex bit)
        secure_bit_hole(
            pc_hex_bit_diameter_small_nominal,
            pc_bit_hole_depth,
            bit_entry_flare_tolerance,
            bit_entry_flare_depth,
            bit_retention_snugness
        ).locate(Location((circ_hole_center_x, circ_hole_bottom_y, hole_cut_z_level)))
    return bit_holes

def create_small_center_hole_cutout() -> Solid:
    """Positions the central hole for a magnet or tiny bit."""
    hole_cut_z_level = recess_top_z_level - small_center_hole_depth - 0.1 # Ensure clean cut

    return Cylinder(
        radius=small_center_hole_diameter / 2,
        height=small_center_hole_depth + 0.2, # Slightly extra depth
        align=(Align.CENTER, Align.CENTER, Align.MIN)
    ).locate(Location((center_recess_x, center_recess_y, hole_cut_z_level)))

def create_side_clip() -> Solid:
    """Defines and positions the robust belt/hip clip."""
    # Define the 2D profile of the clip (side view) using a Sketch.
    # The points define the shape starting from the origin (0,0).
    with BuildSketch() as clip_sketch:
        with Locations((0,0)): # Start sketching at origin
            # Draw the profile of the clip
            Line((0, 0), (0, body_height))
            Line((0, body_height), (clip_projection_length, body_height))
            Line((clip_projection_length, body_height), (clip_projection_length, clip_tip_clearance))
            Line((clip_projection_length, clip_tip_clearance), (clip_projection_length - clip_thickness, clip_tip_clearance))
            Line((clip_projection_length - clip_thickness, clip_tip_clearance), (0,0)) # Close the loop

    # Extrude the 2D sketch to create the 3D clip
    clip_3d = clip_sketch.sketch.face().extrude(clip_width)

    # Position and orient the clip relative to the main body.
    # The clip is attached to the 'right' side of the body (max X).
    return clip_3d.locate(
        Location(
            (body_length - clip_thickness, (body_width - clip_width) / 2, 0), # X, Y, Z translation
            (90, 0, 0) # Rotate 90 degrees around X-axis (aligns sketch Z with model Z)
        )
    )

# --- Final Assembly of the Bit Holder ---
# This is the main assembly block using `build123d`'s object creation and boolean operations.
with Build3D() as screwdriver_bit_holder_assembly:
    main_body = create_main_body()

    # Create all cutouts as separate solids
    top_recess_cut = create_top_recess_cutout()
    rectangular_slots_cut = create_rectangular_tool_slots_cutouts()
    circular_holes_cut = create_circular_bit_holes_cutouts()
    center_hole_cut = create_small_center_hole_cutout()

    # Perform boolean subtraction for all internal features
    main_solid_with_cutouts = main_body.cut(top_recess_cut)
    main_solid_with_cutouts = main_solid_with_cutouts.cut(rectangular_slots_cut)
    main_solid_with_cutouts = main_solid_with_cutouts.cut(circular_holes_cut)
    main_solid_with_cutouts = main_solid_with_cutouts.cut(center_hole_cut)

    # Add the side clip
    side_clip = create_side_clip()
    combined_model = main_solid_with_cutouts.add(side_clip)

# Show the final model (for visualization in CAD viewers or notebooks)
show(screwdriver_bit_holder_assembly.part)
