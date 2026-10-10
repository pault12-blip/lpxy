<?php
// Read the log file into an array of lines
 $lines = @file('feed.log', FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);

if (!$lines) {
    die("Could not read feed.log. Make sure the file exists in the same directory.");
}
?>

<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Feed Log Viewer</title>
    <style>
        body { font-family: monospace; margin: 20px; background: #f4f4f4; }
        table { border-collapse: collapse; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        th, td { padding: 8px 12px; border: 1px solid #ddd; text-align: left; }
        th { background: #f8f8f8; }
        .added { color: green; font-weight: bold; }
        .removed { color: red; font-weight: bold; }
    </style>
</head>
<body>

<h2>Feed Log</h2>
<table>
    <tr>
        <th>Date</th>
        <th>Time</th>
        <th>Provider</th>
        <th>Model</th>
        <th>V1</th>
        <th>V2</th>
    </tr>
    <?php foreach ($lines as $line):
        // Regex to parse: Date Time Provider Model V1 [V2]
        // \S+ matches non-whitespace characters
        if (preg_match('/^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(\S+))?/', trim($line), $matches)):
            
            $dt       = $matches[1];
            $hms      = $matches[2];
            $provider = $matches[3];
            $model    = $matches[4];
            $v1       = $matches[5];
            $v2       = isset($matches[6]) ? $matches[6] : ''; // V2 might be missing on some lines

// Helper function to apply color classes
 $format_val = function($val) {
    $val = htmlspecialchars($val); // Prevent XSS
    if (strpos($val, 'ADDED') !== false) {
        return '<span class="added">' . $val . '</span>';
    } elseif (strpos($val, 'REMOVED') !== false) {
        return '<span class="removed">' . $val . '</span>';
    }
    return $val; // Neutral color for price changes like $5.0000->$4.0000
};

    ?>
    <tr>
        <td><?= htmlspecialchars($dt) ?></td>
        <td><?= htmlspecialchars($hms) ?></td>
        <td><?= htmlspecialchars($provider) ?></td>
        <td><?= htmlspecialchars($model) ?></td>
        <td><?= $format_val($v1) ?></td>
        <td><?= $format_val($v2) ?></td>
    </tr>
    <?php 
        endif;
    endforeach; ?>
</table>

</body>
</html>

