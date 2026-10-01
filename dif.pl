#!/usr/bin/perl
use strict;
use warnings;

my $file = shift or die "Usage: $0 <diff-file>\n";

my %lookup;   # key: "platform provider/model" => { old => [$v1,$v2], new => [$v1,$v2] }

open my $fh, '<', $file or die "Cannot open $file: $!\n";

while (<$fh>) {
    chomp;
    if (/^<\s+(\S+)\s+(\S+)\s+\$(\S+)\s+\$(\S+)/) {
        my ($platform, $model, $v1, $v2) = ($1, $2, $3, $4);
        my $key = "$platform $model";
        $lookup{$key}{old} = [$v1, $v2];
    }
    elsif (/^>\s+(\S+)\s+(\S+)\s+\$(\S+)\s+\$(\S+)/) {
        my ($platform, $model, $v1, $v2) = ($1, $2, $3, $4);
        my $key = "$platform $model";
        $lookup{$key}{new} = [$v1, $v2];
    }
}

close $fh;

my @t   = localtime();
my $dt  = sprintf("%04d-%02d-%02d",   $t[5]+1900, $t[4]+1, $t[3]);
my $hms = sprintf("%02d:%02d:%02d",   $t[2], $t[1], $t[0]);

for my $key (sort keys %lookup) {
    my $e = $lookup{$key};

    my ($platform, $model) = split ' ', $key, 2;
    my ($prov, $mname)     = split m{/}, $model, 2;
    $prov  //= $model;
    $mname //= $model;

    if (exists $e->{old} && exists $e->{new}) {
        my ($o1, $o2) = @{$e->{old}};
        my ($n1, $n2) = @{$e->{new}};
        my @ch;
        push @ch, "\$$o1->\$$n1" if $o1 ne $n1;
        push @ch, "\$$o2->\$$n2" if $o2 ne $n2;
        print "$dt $hms $prov $mname @ch\n" if @ch;
    }
    elsif (exists $e->{old}) {
        my ($o1, $o2) = @{$e->{old}};
        print "$dt $hms $prov $mname \$$o1->REMOVED \$$o2->REMOVED\n";
    }
    else {
        my ($n1, $n2) = @{$e->{new}};
        print "$dt $hms $prov $mname ADDED->\$$n1 ADDED->\$$n2\n";
    }
}

