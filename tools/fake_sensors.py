#!/usr/bin/env python3
"""
Simulador para testes sem a base Create3 ligada: publica <ns>/scan (LaserScan, ~7 Hz, 1080 pontos)
e <ns>/battery_state, e mostra os cmd_vel recebidos.
Uso no robô:  source /etc/turtlebot4/setup.bash && python3 fake_sensors.py [/robot1]
"""
import math
import sys

import rclpy
from geometry_msgs.msg import TwistStamped
from rclpy.node import Node
from sensor_msgs.msg import BatteryState, LaserScan

NS = sys.argv[1] if len(sys.argv) > 1 else "/robot1"


class Fake(Node):
    def __init__(self):
        super().__init__("fake_sensors")
        self.scan_pub = self.create_publisher(LaserScan, f"{NS}/scan", 10)
        self.bat_pub = self.create_publisher(BatteryState, f"{NS}/battery_state", 10)
        self.create_subscription(TwistStamped, f"{NS}/cmd_vel", self.on_cmd, 10)
        self.k = 0
        self.create_timer(1 / 7, self.scan)
        self.create_timer(1.0, self.battery)

    def scan(self):
        n = 1080
        m = LaserScan()
        m.header.stamp = self.get_clock().now().to_msg()
        m.header.frame_id = "rplidar_link"
        m.angle_min, m.angle_max = -math.pi, math.pi
        m.angle_increment = 2 * math.pi / n
        m.range_min, m.range_max = 0.15, 12.0
        self.k += 1
        m.ranges = [float("inf") if i % 97 == 0 else 1.0 + 0.5 * math.sin(i * 0.01 + self.k * 0.1) for i in range(n)]
        self.scan_pub.publish(m)

    def battery(self):
        b = BatteryState()
        b.percentage, b.voltage = 0.87, 15.9
        self.bat_pub.publish(b)

    def on_cmd(self, msg: TwistStamped):
        self.get_logger().info(f"cmd_vel v={msg.twist.linear.x:.3f} w={msg.twist.angular.z:.3f}")


rclpy.init()
rclpy.spin(Fake())
