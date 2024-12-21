#!/usr/bin/env python3

import pandas as pd
import numpy as np
from scipy import stats
import argparse
from datetime import datetime

def bytes_to_human_readable(bytes_value):
    """Convert bytes to human readable format."""
    units = ['B/s', 'KB/s', 'MB/s', 'GB/s', 'TB/s']
    index = 0
    while bytes_value >= 1024 and index < len(units) - 1:
        bytes_value /= 1024
        index += 1
    return f"{bytes_value:.2f} {units[index]}"

def load_and_prepare_data(file_path):
    """Load the CSV file and prepare data for analysis."""
    df = pd.read_csv(file_path)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    return df

def calculate_basic_stats(df, group_by='device_class'):
    """Calculate basic statistics grouped by device class."""
    # First calculate all stats
    stats = df.groupby(group_by).agg({
        'bytes_per_sec': ['mean', 'median', 'std', 'min', 'max'],
        'iops': ['mean', 'median', 'std', 'min', 'max']
    })
    
    # Convert bytes_per_sec to human readable format
    for stat in ['mean', 'median', 'std', 'min', 'max']:
        stats[('bytes_per_sec', stat)] = stats[('bytes_per_sec', stat)].apply(bytes_to_human_readable)
    
    # Round IOPS values
    for stat in ['mean', 'median', 'std', 'min', 'max']:
        stats[('iops', stat)] = stats[('iops', stat)].round(2)
    
    # Adjust display settings
    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', None)
    pd.set_option('display.expand_frame_repr', False)
    pd.set_option('display.max_rows', None)
    
    return stats

def calculate_aggregate_stats(df):
    """Calculate total bandwidth and IOPS per run for each device class."""
    # Group by device class and run number
    agg_stats = df.groupby(['device_class', 'run_number']).agg({
        'bytes_per_sec': 'sum',
        'iops': 'sum'
    }).reset_index()
    
    # Convert bytes_per_sec to human readable format
    agg_stats['bytes_per_sec'] = agg_stats['bytes_per_sec'].apply(bytes_to_human_readable)
    # Round IOPS to 2 decimal places
    agg_stats['iops'] = agg_stats['iops'].round(2)
    
    return agg_stats

def identify_outliers(df, percentage_threshold=15):
    """Identify drives consistently performing below threshold across runs."""
    outliers = {}
    
    for device_class in df['device_class'].unique():
        class_df = df[df['device_class'] == device_class]
        
        # Calculate median performance for the device class
        median_iops = class_df.groupby('run_number')['iops'].median()
        median_throughput = class_df.groupby('run_number')['bytes_per_sec'].median()
        
        # Track underperforming instances for each OSD
        poor_performing_counts = {}
        total_runs = len(df['run_number'].unique())
        
        # Check each OSD's performance in each run
        for osd_id in class_df['osd_id'].unique():
            osd_data = class_df[class_df['osd_id'] == osd_id]
            underperforming_runs = 0
            
            for run in df['run_number'].unique():
                run_data = osd_data[osd_data['run_number'] == run]
                if not run_data.empty:
                    # Check if performance is below threshold for this run
                    if (run_data['iops'].iloc[0] < median_iops[run] * (1 - percentage_threshold/100) or
                        run_data['bytes_per_sec'].iloc[0] < median_throughput[run] * (1 - percentage_threshold/100)):
                        underperforming_runs += 1
            
            # If OSD underperforms in majority of runs (>50%), flag it
            if underperforming_runs > total_runs / 2:
                poor_performing_counts[osd_id] = underperforming_runs
        
        if poor_performing_counts:
            outliers[device_class] = list(poor_performing_counts.keys())
    
    return outliers

def generate_report(file_path, percentage_threshold=15):
    """Generate a comprehensive analysis report."""
    df = load_and_prepare_data(file_path)
    
    print("\n=== Ceph OSD Performance Analysis Report ===")
    print(f"Analysis Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Input File: {file_path}")
    print(f"\nTotal OSDs analyzed: {len(df['osd_id'].unique())}")
    print(f"Device Classes: {', '.join(df['device_class'].unique())}")
    print(f"Number of runs: {len(df['run_number'].unique())}")
    
    print("\n=== Basic Statistics by Device Class ===")
    stats_df = calculate_basic_stats(df)
    print(stats_df.to_string())
    
    print("\n=== Aggregate Performance by Run ===")
    agg_stats = calculate_aggregate_stats(df)
    for device_class in df['device_class'].unique():
        print(f"\n{device_class.upper()} Devices:")
        device_stats = agg_stats[agg_stats['device_class'] == device_class]
        for _, row in device_stats.iterrows():
            print(f"Run {int(row['run_number'])}: Total Bandwidth: {row['bytes_per_sec']}, Total IOPS: {row['iops']}")
    
    print("\n=== Potential Performance Issues ===")
    outliers = identify_outliers(df, percentage_threshold)
    if outliers:
        for device_class, osds in outliers.items():
            print(f"\nPotential slow drives ({device_class}):")
            for osd_id in osds:
                osd_data = df[df['osd_id'] == osd_id]
                avg_iops = osd_data['iops'].mean()
                avg_throughput = bytes_to_human_readable(osd_data['bytes_per_sec'].mean())
                print(f"OSD.{osd_id}: Avg IOPS: {avg_iops:.2f}, Avg Throughput: {avg_throughput}")
    else:
        print("No significant performance outliers detected.")

def main():
    parser = argparse.ArgumentParser(description='Analyze Ceph OSD benchmark results')
    parser.add_argument('file', help='Path to the benchmark CSV file')
    parser.add_argument('--threshold', type=float, default=15,
                       help='Percentage below median to flag as slow (default: 15)')
    
    args = parser.parse_args()
    generate_report(args.file, args.threshold)

if __name__ == '__main__':
    main()
