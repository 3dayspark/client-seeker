import os
import sys
import importlib.metadata
from pathlib import Path

def get_dir_size(path):
    """Calculates the total size of a directory or file."""
    total_size = 0
    if not os.path.exists(path):
        return 0
    if os.path.isfile(path):
        return os.path.getsize(path)

    for dirpath, dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if not os.path.islink(fp): # Avoid counting symlinks as their target's size
                try:
                    total_size += os.path.getsize(fp)
                except FileNotFoundError:
                    # File might have been removed during traversal
                    pass
    return total_size

def get_installed_package_sizes():
    """Gets sizes of all installed packages by trying to find their root directory in site-packages."""
    package_sizes = {}
    
    # Get all potential site-packages paths from sys.path
    site_packages_roots = [Path(p) for p in sys.path if 'site-packages' in p and Path(p).is_dir()]
    
    if not site_packages_roots:
        print("Could not find site-packages directories. Is your virtual environment activated?")
        return []

    # Get a mapping of normalized package names to their Distribution objects
    name_to_dist = {d.metadata['Name'].lower().replace('-', '_'): d for d in importlib.metadata.distributions()}
    
    print(f"Scanning packages in: {[str(p) for p in site_packages_roots]}")

    for sp_root in site_packages_roots:
        for item in sp_root.iterdir():
            if item.is_dir():
                # Check for direct package folders (e.g., 'fastapi', 'pydantic')
                normalized_folder_name = item.name.lower().replace('-', '_')
                if normalized_folder_name in name_to_dist:
                    package_name = name_to_dist[normalized_folder_name].metadata['Name']
                    if package_name not in package_sizes: # Only add if not already processed (e.g., from another site-packages)
                        size = get_dir_size(str(item))
                        package_sizes[package_name] = size
                
                # Check for .dist-info directories (and infer the package folder next to it)
                elif item.name.endswith('.dist-info'):
                    # Extract package name from 'fastapi-0.116.1.dist-info' -> 'fastapi'
                    package_name_parts = item.name.split('-')
                    if package_name_parts:
                        potential_package_name = package_name_parts[0]
                        normalized_potential_package_name = potential_package_name.lower().replace('-', '_')
                        
                        if normalized_potential_package_name in name_to_dist:
                            actual_package_name = name_to_dist[normalized_potential_package_name].metadata['Name']
                            
                            if actual_package_name not in package_sizes:
                                # Try to find the actual package directory (e.g., 'fastapi') next to the .dist-info
                                candidate_package_dir = sp_root / actual_package_name.replace('-', '_')
                                if candidate_package_dir.is_dir():
                                    size = get_dir_size(str(candidate_package_dir))
                                    package_sizes[actual_package_name] = size
                                else:
                                    # Fallback: if no direct package folder, use the dist-info folder as a proxy
                                    size = get_dir_size(str(item))
                                    package_sizes[actual_package_name] = size

            elif item.is_file() and item.suffix in ['.py', '.egg']:
                # Handle single .py files or .egg files at the root of site-packages
                normalized_file_name_base = item.stem.lower().replace('-', '_')
                if normalized_file_name_base in name_to_dist:
                    package_name = name_to_dist[normalized_file_name_base].metadata['Name']
                    if package_name not in package_sizes:
                        size = get_dir_size(str(item))
                        package_sizes[package_name] = size

    sorted_package_sizes = sorted(package_sizes.items(), key=lambda item: item[1], reverse=True)
    return sorted_package_sizes

if __name__ == "__main__":
    print("Getting installed package sizes (this may take a moment)...")
    sizes = get_installed_package_sizes()
    if sizes:
        print("\nInstalled Packages by Size (Largest to Smallest):")
        for name, size_bytes in sizes:
            size_mb = size_bytes / (1024 * 1024)
            print(f"- {name}: {size_mb:.2f} MB")
    else:
        print("No package sizes could be retrieved. This might happen if packages are installed in non-standard ways, or if the script needs further refinement for your environment.")
