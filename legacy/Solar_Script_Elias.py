import numpy as np
import pandas as pd
from joblib import load

sun = pd.read_csv("sun.csv", parse_dates=["date", "timestamp_sunrise", "timestamp_sundown", "timestamp_noon"])

# Load forecast
# forecast = pd.read_excel("datasets/forecastin.xlsx", parse_dates=["timestamp"])
forecast = pd.read_csv("datasets/forecast(in).csv", parse_dates=["timestamp"])
forecast = forecast.set_index("timestamp").tz_localize("Europe/Brussels")

forecast["date"] = forecast.index.date
forecast['date'] = pd.to_datetime(forecast['date'])
forecast = forecast.reset_index()

# Merge data
df = pd.merge(forecast, sun, on="date", how="left")

# Add features
df["daytime"] = np.where((df["timestamp"] >= df["timestamp_sunrise"]) & (df["timestamp"] <= df["timestamp_sundown"]), 1, 0)
df["time_from_noon"] = np.where(df["timestamp"] < df["timestamp_noon"], 
                           (df["timestamp_noon"] - df["timestamp"]).dt.total_seconds() / 60,
                           (df["timestamp"] - df["timestamp_noon"]).dt.total_seconds() / 60)
df["sunlight_time"] = (df["timestamp_sundown"] - df["timestamp_sunrise"]).dt.total_seconds() / 60

def get_season(month):
    if month in [3, 4, 5]:
        return '0'
    elif month in [6, 7, 8]:
        return '1'
    elif month in [9, 10, 11]:
        return '2'
    else:
        return '3'

df['season'] = df['timestamp'].dt.month.apply(get_season)
df['month'] = df['timestamp'].dt.month
df["hour"] = df["timestamp"].dt.hour

# Select relevant columns
df = df[["temp", "humidity_relative", "pressure", "cloudiness", "season", "month", "daytime", "sunlight_time", "time_from_noon", "hour"]]

# Load model
model = load("model.joblib")

# Predict
predictions = model.predict(df)

# Print predictions
for timestamp, prediction in zip(forecast["timestamp"], predictions):
    print(f"Voorspelling voor {timestamp.strftime('%H:%M')}u: {abs(prediction):.2f} kWh")
